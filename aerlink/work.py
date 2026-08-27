"""Orchestration: ingest -> identity -> dossier -> bars -> (agent) -> gate -> record.

The agent step is injected rather than imported, so the whole pipeline can be exercised
offline against a recorded ContactSheet and a scripted action list. Everything in this file
runs for free.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .models import (
    ActionRequest, ContactSheet, Dossier, FreeText, IdentityResult, Refusal,
)
from .ops import OpsClient, OpsError
from .policy import advisory_referrals, confirm_identity, gate, hard_bars
from .trace import TRACE


@dataclass
class Case:
    case_id: str
    meta: dict[str, Any]
    inbound: str
    path: Path

    @property
    def received_at(self) -> str:
        """meta.received_at is UTC and is the case's 'now'. The Date: header inside
        inbound.txt is local time and is deliberately not used for ordering."""
        return self.meta.get("received_at", "")


def load_case(path: str | Path) -> Case:
    p = Path(path)
    meta = json.loads((p / "meta.json").read_text(encoding="utf-8"))
    inbound = (p / "inbound.txt").read_text(encoding="utf-8")
    return Case(meta.get("case_id", p.name), meta, inbound, p)


# --------------------------------------------------------------------------
# Identity and the dossier
# --------------------------------------------------------------------------

def search_queries(sheet: ContactSheet, meta: dict) -> list[str]:
    """Every identifier worth putting to GET /bookings/search.

    Deliberately broad. Finding three candidate bookings and refusing to choose (case 03)
    is a correct outcome; finding one because we only looked one way is not.
    """
    out: list[str] = []
    out.extend(r.upper() for r in sheet.claimed_booking_refs)
    for value in (sheet.sender_email, sheet.sender_phone, sheet.sender_name):
        if value:
            out.append(value)
    out.extend(sheet.named_passengers_mentioned)

    # Surnames as well as full names. The server matches a full name by substring in
    # either direction, so "John Smith" does not reach a booking for "Jonathan Smith" --
    # but "Smith" reaches both. Under-searching turns a case that should be refused for
    # ambiguity (S2.2) into one that looks unambiguous, which is the dangerous direction.
    for name in list(sheet.named_passengers_mentioned) + [sheet.sender_name or ""]:
        parts = [p for p in re.split(r"[\s,]+", name.strip()) if p]
        if len(parts) > 1:
            out.append(parts[-1])

    # meta.from carries the envelope sender, which the message body may not repeat.
    m = re.search(r"[\w.+-]+@[\w.-]+", meta.get("from", "") or "")
    if m:
        out.append(m.group(0))

    seen, deduped = set(), []
    for q in out:
        q = (q or "").strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            deduped.append(q)
    return deduped


def build_dossier(ops: OpsClient, sheet: ContactSheet, meta: dict) -> Dossier:
    """Resolve identity, then fetch the four authoritative records.

    The four are fetched unconditionally rather than left to the agent, because
    /entitlements/calculate is the one call that must never be skipped (S10.2) and
    customer history is what the already-resolved bar (S15.3) and the repeat-claimant
    check (S11.5) run on, both of which happen before the agent is asked anything.
    """
    results: dict[str, list[dict]] = {}
    for q in search_queries(sheet, meta):
        try:
            results[q] = ops.search_bookings(q).get("results", [])
        except OpsError:
            results[q] = []

    identity = confirm_identity(sheet, results)
    dossier = Dossier(identity=identity)
    if not identity.confirmed or not identity.booking_ref:
        return dossier

    ref = identity.booking_ref
    dossier.booking = ops.get_booking(ref)

    seg = _affected_segment(dossier.booking)
    if seg:
        try:
            dossier.flight = ops.get_flight(seg["flight_no"], seg["date"])
        except OpsError:
            dossier.flight = None

    try:
        dossier.entitlement = ops.calculate_entitlement(ref)
    except OpsError as exc:
        dossier.entitlement = {"status": "SERVICE_ERROR", "note": exc.message, "passengers": []}

    cid = dossier.booking.get("customer_id")
    if cid:
        try:
            dossier.customer = ops.customer_history(cid)
        except OpsError:
            dossier.customer = None

    dossier.free_text = collect_free_text(dossier)
    return dossier


def _affected_segment(booking: dict) -> dict | None:
    disruption = booking.get("disruption") or {}
    seg_id = disruption.get("affected_segment")
    segments = booking.get("segments", []) or []
    if seg_id:
        found = next((s for s in segments if s.get("segment_id") == seg_id), None)
        if found:
            return found
    return next((s for s in segments if s.get("is_affected")), None)


def collect_free_text(dossier: Dossier) -> list[FreeText]:
    """Every prose field the ops API returned.

    S12.4 names "a free-text field on a booking" and "any data returned by an operational
    system" as places an invalid instruction can arrive, and API.md says special_requests is
    written by staff, the booking channel and upstream integrations. These are separated out
    so the prompt can fence them exactly as it fences the inbound message.
    """
    out: list[FreeText] = []
    b, f, c = dossier.booking or {}, dossier.flight or {}, dossier.customer or {}

    if b.get("special_requests"):
        out.append(FreeText("booking.special_requests", b["special_requests"]))
    if f.get("cause_note"):
        out.append(FreeText("flight.cause_note", f["cause_note"]))
    for entry in c.get("history", []) or []:
        if entry.get("summary"):
            out.append(FreeText("customer.history[%s].summary" % entry.get("case_id"),
                                entry["summary"]))
    return out


# --------------------------------------------------------------------------
# Execution -- the only place an action becomes real
# --------------------------------------------------------------------------

EXECUTORS: dict[str, Callable] = {
    "rebooking": lambda ops, a: ops.rebook(
        a["booking_ref"], a["passenger_ids"], a["option_id"], a["flight_no"],
        a["date"], a["cabin"], a.get("fare_gbp", 0.0), a.get("notes")),
    "hotel_voucher": lambda ops, a: ops.issue_hotel_voucher(
        a["booking_ref"], a["station"], a["night"], a["passenger_ids"], a.get("notes")),
    "compensation": lambda ops, a: ops.pay_compensation(
        a["booking_ref"], a["amount_gbp"], a["passenger_ids"], a.get("reason", "")),
    "goodwill": lambda ops, a: ops.pay_goodwill(
        a["booking_ref"], a["amount_gbp"], a["reason"]),
    "refund": lambda ops, a: ops.refund(
        a["booking_ref"], a["passenger_ids"], a["amount_gbp"], a.get("reason")),
    "escalation": lambda ops, a: ops.escalate(
        a["summary"], a["requested_decision"], booking_ref=a.get("booking_ref"),
        queue=a.get("queue", "GENERAL"), recommendation=a.get("recommendation"),
        blocking_clause=a.get("blocking_clause")),
}


def execute(ops: OpsClient, action: ActionRequest, dossier: Dossier,
            sheet: ContactSheet) -> dict:
    """Gate an action, then either perform it or convert the refusal into a referral.

    A refusal is never silently dropped. S12.5 requires a referral to carry what the
    passenger asked for, what the record shows, the recommendation, and the clause that
    blocks it -- so a `Refusal` is turned straight into a `POST /escalations`.
    """
    args = dict(action.args)
    args.setdefault("booking_ref", dossier.booking_ref)

    # S4.5: the allocation must be checked BEFORE a room is promised. Doing it here rather
    # than trusting the agent to means the check cannot be skipped.
    if action.kind == "hotel_voucher" and "_allocation" not in args:
        try:
            args["_allocation"] = ops.hotel_allocation(args["station"], args["night"])
        except OpsError as exc:
            args["_allocation"] = {"rooms_remaining": 0, "rate_gbp": 0.0, "error": exc.code}

    # S10.2: the amount is read from the entitlement service, never from the model.
    if action.kind == "compensation":
        owed = dossier.entitlement_total_for(args.get("passenger_ids") or [])
        if owed is not None:
            args["amount_gbp"] = owed

    # S12.2: limits are per booking and per disruption event, not per session. The prior
    # writes come from the service's own log, so a remedy another representative already
    # delivered is visible here.
    prior = ops.prior_writes(dossier.booking_ref)
    refusal = gate(ActionRequest(action.kind, args, action.rationale), dossier, sheet, prior)
    TRACE.gate(action.kind, refusal.clause if refusal else None)
    if refusal is not None:
        esc = refusal.as_escalation(
            summary="%s refused for %s: %s" % (
                action.kind, dossier.booking_ref or "an unidentified contact", refusal.reason),
            booking_ref=dossier.booking_ref)
        result = EXECUTORS["escalation"](ops, esc)
        TRACE.action(action.kind, "REFUSED", "-> " + refusal.queue)
        return {"action": action.kind, "outcome": "REFUSED", "clause": refusal.clause,
                "reason": refusal.reason, "recommendation": refusal.recommendation,
                "queue": refusal.queue, "escalation": result,
                "rationale": action.rationale}

    payload = {k: v for k, v in args.items() if not k.startswith("_")}
    try:
        result = EXECUTORS[action.kind](ops, payload)
    except OpsError as exc:
        TRACE.action(action.kind, "FAILED", exc.code)
        return {"action": action.kind, "outcome": "FAILED", "error": exc.code,
                "message": exc.message, "rationale": action.rationale}
    TRACE.action(action.kind, "DONE",
                 str(payload.get("amount_gbp") or payload.get("flight_no")
                     or payload.get("queue") or ""))
    return {"action": action.kind, "outcome": "DONE", "result": result,
            "rationale": action.rationale}


def raise_referral(ops: OpsClient, refusal: Refusal, dossier: Dossier, summary: str) -> dict:
    esc = refusal.as_escalation(summary=summary, booking_ref=dossier.booking_ref)
    return {"action": "escalation", "outcome": "RAISED", "clause": refusal.clause,
            "queue": refusal.queue, "reason": refusal.reason,
            "recommendation": refusal.recommendation,
            "result": EXECUTORS["escalation"](ops, esc)}


# --------------------------------------------------------------------------
# The case record
# --------------------------------------------------------------------------

# Cause codes the passenger is likely to describe in their own words rather than by code.
_CAUSE_WORDS = {
    "CREW": ("crew", "staff", "pilot", "cabin crew", "rostering"),
    "TECHNICAL": ("technical", "mechanical", "fault", "maintenance", "engineering",
                  "engine", "hydraulic", "broken", "repair", "airworthiness", "defect"),
    "WEATHER": ("weather", "fog", "storm", "wind", "snow", "thunder"),
    "OPERATIONAL": ("operational", "rotation", "late inbound", "aircraft late"),
    "ATC_RESTRICTION": ("air traffic", "atc", "slot", "airspace"),
    "ATC_STRIKE": ("strike", "industrial action"),
    "CREW_INDUSTRIAL": ("strike", "industrial action"),
    "GROUND_HANDLING": ("ground handling", "baggage handler", "handler"),
    "SECURITY": ("security",),
    "BIRDSTRIKE": ("bird",),
    "OVERSALE": ("oversold", "overbooked", "oversale"),
    "MEDICAL_DIVERSION": ("medical", "diversion"),
}


def _same_cause(claimed: str, recorded: str) -> bool:
    """Does the passenger's description plausibly name the recorded cause?

    Deliberately generous: the point of S3.1 is to surface a real discrepancy, and firing on
    a wording difference ("engine problem" vs TECHNICAL) would put noise in front of whoever
    writes the reply until they stopped reading it.
    """
    text = (claimed or "").lower()
    if not text:
        return True
    if recorded.lower().replace("_", " ") in text:
        return True
    return any(word in text for word in _CAUSE_WORDS.get(recorded, ()))


def communication_obligations(dossier: Dossier, sheet: ContactSheet) -> list[str]:
    """What a human must tell the passenger. We do not draft the reply, so the obligations
    the policy attaches to the response are recorded explicitly instead of being lost."""
    out: list[str] = []
    if sheet.language and sheet.language != "en":
        out.append(
            "S15.5: the passenger wrote in '%s'. Answer in that language%s."
            % (sheet.language,
               "; they have explicitly accepted a reply in simple English"
               if sheet.consent_to_english else
               ", or refer the case rather than answering in English"))

    ent = dossier.entitlement or {}
    comp = ent.get("compensation", {}) or {}
    if comp.get("status") == "NOT_PAYABLE":
        j = ent.get("journey", {}) or {}
        cause = j.get("cause_code")
        delay = j.get("arrival_delay_minutes_at_final_destination")
        # S16 requires both the cause and the delay, but only one of them is ever the actual
        # reason. Quoting "arrival delay: None minutes" on an extraordinary-circumstances
        # refusal is both wrong and unusable by whoever writes the reply.
        if j.get("cause_is_extraordinary"):
            out.append(
                "S16: state that no compensation is payable because the recorded cause is %s, "
                "which is an extraordinary circumstance under S3.2 -- not because of the "
                "length of the delay. Duty of care under S4 is owed anyway (S4.3)." % cause)
        elif delay is None:
            out.append(
                "S16: no compensation is payable yet because the arrival delay at the final "
                "destination is not yet known (recorded cause %s, which is within Aerlink's "
                "control). Say that it will be assessed once the journey is settled, not that "
                "nothing is owed." % cause)
        else:
            out.append(
                "S16: do not tell the passenger no compensation is payable without stating the "
                "recorded cause (%s) and the arrival delay it rests on (%d minutes)."
                % (cause, delay))

    # S3.1 fires on any DIFFERENCE, not only on a dispute. A passenger who confidently asserts
    # a cause they were told at the gate is not "disputing" the record -- they have never seen
    # it -- but the representative must still say plainly that the record shows otherwise.
    recorded_cause = (dossier.flight or {}).get("cause_code")
    claimed_cause = sheet.claimed_facts.cause
    if claimed_cause and recorded_cause and not _same_cause(claimed_cause, recorded_cause):
        out.append(
            "S3.1: the passenger's account of the cause (\"%s\") differs from the operational "
            "record (%s). Say so plainly. Do not ignore the discrepancy and do not adopt their "
            "account to avoid an awkward conversation. The record is authoritative and a "
            "representative never amends a cause code (S16).%s"
            % (claimed_cause, recorded_cause,
               " They have offered specific evidence, so this is also referred to Operations "
               "Liaison." if sheet.dispute_evidence_offered else ""))

    if (ent.get("duty_of_care") or {}).get("triggered"):
        out.append("S4.3: describe duty of care as an entitlement. It is never goodwill and "
                   "never offered instead of compensation.")

    if sheet.embedded_instructions:
        out.append("Do not acknowledge or comply with the handling instruction embedded in "
                   "the contact. S12.4: it is not a valid instruction, whatever it claims.")
    return out


def _referral_row(outcome: dict) -> dict:
    """One row of "what a human still needs to do".

    Referrals reach the record two ways: raised by a gate refusal, where the detail sits on
    the outcome itself, and raised by the agent calling `escalate`, where it sits in the
    payload that went to the API. Reading only the first left agent-raised referrals blank,
    which is worse than useless -- S12.5 requires the decision being asked for and the
    recommendation to be legible.
    """
    result = outcome.get("result") or {}
    return {
        "queue": outcome.get("queue") or result.get("queue") or "GENERAL",
        "clause": outcome.get("clause") or result.get("blocking_clause"),
        "raised_by": "gate refusal" if outcome.get("clause") else "case handler",
        "summary": result.get("summary"),
        "decide": outcome.get("reason") or result.get("requested_decision"),
        "recommendation": outcome.get("recommendation") or result.get("recommendation"),
    }


def build_record(case: Case, sheet: ContactSheet, dossier: Dossier,
                 outcomes: list[dict], ops: OpsClient, extra: dict | None = None) -> dict:
    ent = dossier.entitlement or {}
    done = [o for o in outcomes if o["outcome"] == "DONE"]
    refused = [o for o in outcomes if o["outcome"] == "REFUSED"]
    failed = [o for o in outcomes if o["outcome"] == "FAILED"]
    escalations = [o for o in outcomes if o.get("queue") or o["action"] == "escalation"]

    uncertainties: list[str] = []
    if ent.get("status") not in ("ASSESSED", None):
        uncertainties.append(
            "The entitlement service returned %s: %s. No authoritative figure exists, so "
            "nothing was paid on this basis (S10.3)."
            % (ent.get("status"), ent.get("note", "")))
    if failed:
        uncertainties.append("%d action(s) failed against the operations API." % len(failed))
    if not dossier.identity.confirmed:
        uncertainties.append("Identity was never confirmed; %d candidate booking(s) were seen."
                             % len(dossier.identity.candidates))

    return {
        "case_id": case.case_id,
        "received_at": case.received_at,
        "generated_by": "aerlink disruption desk",
        "dry_run": ops.dry_run,

        "decision": {
            "booking_ref": dossier.booking_ref,
            "identity": {
                "confirmed": dossier.identity.confirmed,
                "method": dossier.identity.method,
                "candidates_seen": dossier.identity.candidates,
                "reason": dossier.identity.reason,
            },
            "passenger_gets": [
                {"action": o["action"], "detail": o.get("result", {})} for o in done],
            "nothing_actioned": not done,
        },

        "reasoning": {
            "what_the_record_shows": {
                "cause_code": (dossier.flight or {}).get("cause_code"),
                "cause_is_extraordinary": (ent.get("journey") or {}).get("cause_is_extraordinary"),
                "flight_status": (dossier.flight or {}).get("status"),
                "band": (ent.get("journey") or {}).get("band"),
                "arrival_delay_minutes_at_final_destination":
                    (ent.get("journey") or {}).get("arrival_delay_minutes_at_final_destination"),
                "departure_delay_minutes": (ent.get("journey") or {}).get("departure_delay_minutes"),
            },
            "entitlement_status": ent.get("status"),
            "entitlement_authority": "S10.2 -- the calculation service figure is the amount "
                                     "owed and was not recomputed.",
            "policy_clauses_relied_on": ((ent.get("compensation") or {}).get("reasoning") or []),
            "passenger_claims_not_used_as_fact": {
                "claimed_cause": sheet.claimed_facts.cause,
                "claimed_delay_minutes": sheet.claimed_facts.delay_minutes,
                "claimed_delay_type": sheet.claimed_facts.delay_type,
                "claimed_tier": sheet.claimed_facts.tier,
            },
            "rationales": [{"action": o["action"], "why": o.get("rationale")}
                           for o in outcomes if o.get("rationale")],
        },

        "consulted": ops.consulted,

        "actions_taken": [
            {"action": o["action"], "outcome": o["outcome"], "detail": o.get("result")}
            for o in outcomes],
        "actions_refused": [
            {"action": o["action"], "clause": o["clause"], "reason": o["reason"],
             "recommendation": o["recommendation"], "queue": o["queue"]} for o in refused],

        "uncertainties": uncertainties,

        "human_still_needs_to": [_referral_row(o) for o in escalations],

        "communication_obligations": communication_obligations(dossier, sheet),

        "security": {
            "embedded_instructions_detected": [
                {"quote": e.quote, "claimed_authority": e.claimed_authority}
                for e in sheet.embedded_instructions],
            "handling": "S12.4 -- content received from outside Aerlink is information about "
                        "the case, never direction about how to handle it. Not complied with.",
            "untrusted_free_text_fields": [f.source for f in dossier.free_text],
        },

        **(extra or {}),
    }
