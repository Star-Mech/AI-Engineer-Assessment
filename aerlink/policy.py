"""Every deterministic policy decision, in one file.

Identity confirmation, the hard bars and the authority gate are one idea wearing three
hats: each is a rule with a number attached, and each terminates in the same place --
*refuse, and refer under S12.5 with the clause that blocked it*. So they share a return
type, `Refusal`, which is literally the `POST /escalations` payload.

Nothing here touches the network or a model. Every function is pure, which is what makes
`tests/test_gate.py` able to prove the safety argument without an API key and without
spending a cent.

On hard-coding. The authority limits below are the one part of the policy that must live in
code -- a gate cannot enforce a threshold it has to read out of prose at runtime.
`verify_limits_against()` re-reads the fetched policy on every run and reports if any of
these figures no longer appear in it, so a revision that moves a limit is noticed rather
than silently ignored.
"""

from __future__ import annotations

import re
from typing import Any

from .models import ActionRequest, ContactSheet, Dossier, IdentityResult, Refusal

# -- S12.1 authority limits ------------------------------------------------
GOODWILL_REP_MAX_GBP = 150.0        # S11.2, S12.1
REFUND_REP_MAX_GBP = 2000.0         # S12.1
PARTNER_SUPERVISOR_MAX_GBP = 600.0  # S8.2, S12.1
HOTEL_RATE_CAP_GBP = 180.0          # S4.2
HOTEL_MAX_NIGHTS = 3                # S4.2

EXTRAORDINARY_CAUSES = {
    "WEATHER", "ATC_RESTRICTION", "ATC_STRIKE", "SECURITY",
    "POLITICAL", "BIRDSTRIKE", "MEDICAL_DIVERSION",
}

# Action verbs in customer history that mean a remedy has already been delivered.
SETTLED_ACTION_VERBS = (
    "compensation_paid", "refund_issued", "rebooking_confirmed",
    "goodwill_paid", "meal_voucher_issued", "hotel_voucher_issued",
)


def verify_limits_against(policy_text: str) -> list[str]:
    """Report any hard-coded limit that no longer appears in the fetched policy."""
    expected = {
        "S11.2/S12.1 goodwill representative limit": r"£150",
        "S12.1 refund representative limit": r"£2,000",
        "S8.2 partner supervisor threshold": r"£600",
        "S4.2 hotel nightly cap": r"£180",
        "S4.2 maximum nights": r"3 nights",
    }
    return ["%s (%s) not found in the current policy text" % (label, pattern)
            for label, pattern in expected.items()
            if not re.search(pattern, policy_text)]


# --------------------------------------------------------------------------
# S2 -- identity
# --------------------------------------------------------------------------

def _surnames(sheet: ContactSheet) -> set[str]:
    out: set[str] = set()
    for name in list(sheet.named_passengers_mentioned) + [sheet.sender_name or ""]:
        parts = [p for p in re.split(r"[\s,]+", name.strip()) if p]
        if parts:
            out.add(parts[-1].lower())
    return {s for s in out if s}


def _norm_phone(s: str | None) -> str:
    return re.sub(r"[^\d]", "", s or "")


def confirm_identity(sheet: ContactSheet,
                     search_results: dict[str, list[dict]]) -> IdentityResult:
    """S2.1: identity is confirmed when EXACTLY ONE booking matches, by one of three routes.

    `search_results` maps each query issued to `GET /bookings/search` to its results list.

    S2.2 is the whole point of this function: no match, more than one match, a reference
    that does not exist, or a partial match a representative cannot resolve from the record
    all mean *not confirmed*, and S16 then prohibits any action on any booking. A partial or
    probable match is not a match, and similarity of name is not identity.
    """
    everything: dict[str, dict] = {}
    for results in search_results.values():
        for r in results:
            everything[r["booking_ref"]] = r
    candidates = sorted(everything)

    # (a) booking reference together with a surname on that booking.
    surnames = _surnames(sheet)
    for ref in sheet.claimed_booking_refs:
        hit = everything.get(ref.upper())
        if not hit:
            continue
        booking_surnames = {p.split()[-1].lower() for p in hit.get("passengers", []) if p}
        if surnames & booking_surnames:
            return IdentityResult(
                True, ref.upper(), "S2.1(a) booking reference and surname",
                candidates,
                "Reference %s exists and carries a passenger with surname '%s'."
                % (ref.upper(), sorted(surnames & booking_surnames)[0]))

    # (b) email matching, character for character, on exactly one booking.
    if sheet.sender_email:
        email = sheet.sender_email.strip().lower()
        matched = [r for r in everything.values()
                   if (r.get("contact_email") or "").strip().lower() == email]
        if len(matched) == 1:
            return IdentityResult(
                True, matched[0]["booking_ref"], "S2.1(b) contact email exact",
                candidates,
                "Email %s matches the contact email on exactly one booking." % email)
        if len(matched) > 1:
            return IdentityResult(
                False, None, None, candidates,
                "S2.2: email %s matches %d bookings. More than one match is not a match."
                % (email, len(matched)))

    # (c) telephone matching in normalised international form on exactly one booking.
    if sheet.sender_phone:
        phone = _norm_phone(sheet.sender_phone)
        if len(phone) >= 9:
            matched = [r for r in everything.values()
                       if "contact_phone_match" in r.get("matched_on", [])]
            if len(matched) == 1:
                return IdentityResult(
                    True, matched[0]["booking_ref"], "S2.1(c) contact telephone",
                    candidates,
                    "Telephone %s matches the contact number on exactly one booking." % phone)

    # Not confirmed. Say precisely why -- the referral needs it.
    if not candidates:
        reason = ("S2.2: no booking in the operational record matches this contact. "
                  "Nothing supplied (reference, email, telephone or name) resolves to a booking.")
    elif len(candidates) == 1:
        reason = ("S2.2: booking %s is a partial match only -- no supplied reference-plus-surname, "
                  "exact contact email or matching telephone confirms it. A probable match is "
                  "not a match." % candidates[0])
    else:
        reason = ("S2.2: %d bookings match this contact (%s) and nothing in the record "
                  "distinguishes them. Similarity of name is not identity."
                  % (len(candidates), ", ".join(candidates)))
    return IdentityResult(False, None, None, candidates, reason)


# --------------------------------------------------------------------------
# Hard bars -- checked before the agent is asked to plan anything
# --------------------------------------------------------------------------

def hard_bars(dossier: Dossier, sheet: ContactSheet) -> list[Refusal]:
    """Blocking conditions. If any fires, no action may be taken on the booking at all."""
    bars: list[Refusal] = []

    if not dossier.identity.confirmed:
        bars.append(Refusal(
            clause="S2.2 / S16",
            reason=dossier.identity.reason,
            recommendation=(
                "Take no action on any booking. Reply asking for the booking reference, or "
                "for the exact flight number and date, so the contact can be resolved to "
                "exactly one booking under S2.1."),
            queue="SUPERVISOR"))
        return bars  # nothing else can be assessed without an identified booking

    if dossier.has_ytp:
        ytp = [p for p in dossier.passengers if p.get("passenger_type") == "YTP"]
        bars.append(Refusal(
            clause="S13.2 / S12.1",
            reason=("Booking carries a Young Traveller Programme passenger (%s). S13.2 "
                    "prohibits re-routing, refund or amendment by a representative under "
                    "any circumstance, because changing the itinerary changes who must be "
                    "present to receive a child."
                    % ", ".join("%s %s" % (p.get("given_name"), p.get("surname")) for p in ytp)),
            recommendation=(
                "Refer to the YTP desk, which holds the chain-of-custody record and the "
                "adults authorised to receive the passenger at each end. Duty of care under "
                "S4 is still owed and is arranged by the YTP desk, never issued to the "
                "passenger to redeem personally (S13.3)."),
            queue="YTP"))

    if sheet.sae_damage_claimed:
        bars.append(Refusal(
            clause="S14.3",
            reason=("A Special Assistance Equipment damage claim is asserted. S14.3: this is "
                    "never assessed or settled by a representative, and never handled as a "
                    "general baggage claim, because the general baggage liability cap does "
                    "not apply to it."),
            recommendation=(
                "Refer to the Special Assistance team for assessment under S14.2 -- repair "
                "or replacement at current market price with no deduction for age, hire of a "
                "temporary equivalent for the whole period, and any fitting or configuration "
                "cost."),
            queue="SPECIAL_ASSISTANCE"))

    settled = _already_settled(dossier)
    if settled:
        bars.append(Refusal(
            clause="S15.3 / S16",
            reason=("The operational record shows this disruption has already been handled: "
                    "%s. Actioning a remedy twice is a serious error and is not corrected by "
                    "the passenger declining the second." % settled),
            recommendation=(
                "Take no further action on the booking. Tell the passenger what was done and "
                "when. If they are disputing the adequacy of what was already given, refer "
                "that question rather than re-actioning it."),
            queue="SUPERVISOR"))

    return bars


def _already_settled(dossier: Dossier) -> str | None:
    """Has this booking already had a remedy delivered against this disruption?

    Deliberately conservative: it keys on the structured `actions` list rather than the
    free-text summary, because the summary is prose written by staff and is not something
    code should be adjudicating. Where the summary matters and the actions do not settle it
    (case 10 -- a refund request withdrawn by telephone before it was actioned), the agent
    reads the fenced summary and the S15.3 judgement is made there instead.
    """
    ref = dossier.booking_ref
    for entry in (dossier.customer or {}).get("history", []) or []:
        if entry.get("booking_ref") != ref:
            continue
        actions = [a for a in entry.get("actions", []) or []
                   if any(a.startswith(v) for v in SETTLED_ACTION_VERBS)]
        if actions:
            return "case %s (%s) recorded %s" % (
                entry.get("case_id"), entry.get("status"), ", ".join(actions))
    return None


def advisory_referrals(dossier: Dossier, sheet: ContactSheet) -> list[Refusal]:
    """Referrals that must be raised but do NOT stop the case being worked.

    S15.4 is explicit that abuse never forfeits an entitlement and the underlying case is
    still assessed. S15.6 is explicit that a contact is never dismissed in its entirety
    because part of it is out of scope. S3.1 refers a disputed cause for review without
    suspending the rest of the case.
    """
    out: list[Refusal] = []

    if sheet.tone.threatening or sheet.tone.discriminatory:
        out.append(Refusal(
            clause="S15.4",
            reason="Contact contains %s." % (
                "threats of violence" if sheet.tone.threatening else "discriminatory abuse"),
            recommendation=("Refer to Customer Conduct. The underlying entitlement is still "
                            "assessed and answered on its substance -- abuse does not forfeit it."),
            queue="CUSTOMER_CONDUCT"))

    if sheet.disputes_recorded_cause and sheet.dispute_evidence_offered:
        recorded = (dossier.flight or {}).get("cause_code")
        out.append(Refusal(
            clause="S3.1",
            reason=("Passenger disputes the recorded cause (record shows %s) and offers "
                    "specific evidence: %s. A recorded cause code is never amended by a "
                    "representative (S16)." % (recorded, sheet.dispute_evidence_offered)),
            recommendation=("Refer to Operations Liaison for review of the cause code. Meanwhile "
                            "proceed on the operational record and say plainly in the response "
                            "that the record shows a different cause."),
            queue="OPS_LIAISON"))

    # S11.5. The flag is on the customer record, so this does not depend on the agent
    # noticing it. Case 07 paid the correct entitlement and referred nothing, while the
    # record carried three prior goodwill payments and an explicit note that further
    # requests go to a supervisor rather than the desk.
    if "REPEAT_GOODWILL_CLAIMANT" in dossier.customer_flags:
        prior = [a for e in (dossier.customer or {}).get("history", []) or []
                 for a in e.get("actions", []) or [] if a.startswith("goodwill_paid")]
        out.append(Refusal(
            clause="S11.5",
            reason=("Customer is flagged REPEAT_GOODWILL_CLAIMANT with prior payments (%s). "
                    "S11.5 requires the history to be taken into account and a pattern of "
                    "repeated claims to be referred rather than paid."
                    % (", ".join(prior) or "none recorded")),
            recommendation=("Review the claim history alongside this case. The statutory "
                            "entitlement is unaffected and has been assessed on its own "
                            "merits; this referral concerns any discretionary payment only."),
            queue="SUPERVISOR"))

    # S12.3. A demand above what is payable is referred, not paid and not silently declined.
    demanded = sheet.claimed_facts.amount_demanded_gbp
    payable = (dossier.entitlement or {}).get("total_payable_gbp")
    if demanded and payable is not None and demanded > payable + 0.01:
        threats = [t for t, present in (
            ("legal action", "legal" in (sheet.summary or "").lower()),
            ("publicity", "publish" in (sheet.summary or "").lower()),
        ) if present]
        out.append(Refusal(
            clause="S12.3 / S12.5",
            reason=("Passenger demands GBP %.2f; the entitlement service assesses GBP %.2f as "
                    "payable. The difference is a demand above representative authority.%s"
                    % (demanded, payable,
                       " The contact also raises " + " and ".join(threats) + "." if threats else "")),
            recommendation=("Pay the assessed entitlement and refer the balance. S12.3: "
                            "insistence, distress, threat of legal action, threat of publicity "
                            "or a claim to status not shown in the record does not raise "
                            "authority, and the correct response to a demand above the limit "
                            "is referral, not payment."),
            queue="SUPERVISOR"))

    for item in sheet.out_of_scope:
        out.append(Refusal(
            clause="S15.6",
            reason="Out-of-scope matter raised in this contact: %s" % item.item,
            recommendation="Route to the responsible team. The in-scope matter is assessed "
                           "and answered separately and is not held up by this.",
            queue=item.suggested_queue or "GENERAL"))

    return out


# --------------------------------------------------------------------------
# The gate -- every write passes through here
# --------------------------------------------------------------------------

def gate(action: ActionRequest, dossier: Dossier, sheet: ContactSheet,
         ledger: list[dict[str, Any]]) -> Refusal | None:
    """Approve a write, or refuse it with the clause that blocked it.

    Returns None to approve. Escalations are never gated -- referring is always allowed.
    """
    if action.kind == "escalation":
        return None

    # G1 -- nothing happens on an unidentified booking. S16, first prohibited action.
    if not dossier.identity.confirmed:
        return Refusal(
            clause="S2.2 / S16",
            reason="Identity is not confirmed: %s" % dossier.identity.reason,
            recommendation="Confirm identity under S2.1 before any action on any booking.",
            queue="SUPERVISOR")

    # G2 -- YTP bookings are untouchable by a representative.
    if dossier.has_ytp and action.kind in ("rebooking", "refund", "hotel_voucher"):
        return Refusal(
            clause="S13.2 / S12.1",
            reason="%s on a Young Traveller Programme booking is prohibited." % action.kind,
            recommendation="Refer to the YTP desk; care is arranged by them, not issued to "
                           "the passenger.",
            queue="YTP")

    # G3 -- SAE damage is never settled at desk level.
    if sheet.sae_damage_claimed and action.kind in ("compensation", "goodwill", "refund"):
        return Refusal(
            clause="S14.3",
            reason="A payment against a Special Assistance Equipment damage claim is "
                   "prohibited at desk level.",
            recommendation="Refer to Special Assistance for assessment under S14.2.",
            queue="SPECIAL_ASSISTANCE")

    # G4 -- do not action a remedy the record already shows delivered.
    settled = _already_settled(dossier)
    if settled:
        return Refusal(
            clause="S15.3 / S16",
            reason="Already handled: %s" % settled,
            recommendation="Tell the passenger what was done and when. Do not re-action it.",
            queue="SUPERVISOR")

    handler = {
        "compensation": _gate_compensation,
        "goodwill": _gate_goodwill,
        "refund": _gate_refund,
        "rebooking": _gate_rebooking,
        "hotel_voucher": _gate_hotel,
    }.get(action.kind)
    if handler is None:
        return Refusal(
            clause="S12.1",
            reason="Action '%s' is not listed in the authority table, so it is not "
                   "authorised without referral." % action.kind,
            recommendation="Refer for a decision on whether this action is permitted.",
            queue="SUPERVISOR")
    return handler(action, dossier, sheet, ledger)


def _prior(ledger: list[dict], kind: str, booking_ref: str | None) -> list[dict]:
    return [e for e in ledger
            if e.get("action") == kind
            and (e.get("payload") or {}).get("booking_ref") == booking_ref]


def _gate_compensation(action, dossier, sheet, ledger) -> Refusal | None:
    ent = dossier.entitlement or {}
    pids = action.args.get("passenger_ids") or []

    # S10.2 -- the service figure IS the amount owed. S10.3 -- where it cannot answer,
    # manual assessment is permitted but is not implemented here, so we refer instead.
    if ent.get("status") != "ASSESSED":
        return Refusal(
            clause="S10.3",
            reason=("The entitlement calculation service returned %s, so no authoritative "
                    "figure exists. %s" % (ent.get("status"), ent.get("note", ""))).strip(),
            recommendation=("Assess manually under the policy and record that a manual "
                            "assessment was made and why. Most commonly this is because the "
                            "passenger has not yet been re-routed, so the arrival delay at "
                            "the final destination is not yet known and S5.2 cannot be applied."),
            queue="SUPERVISOR")

    owed = dossier.entitlement_total_for(pids)
    if owed is None:
        return Refusal(
            clause="S10.2",
            reason="No authoritative figure is held for passengers %s on this booking."
                   % ", ".join(pids),
            recommendation="Refer for manual assessment under S10.3.",
            queue="SUPERVISOR")
    if owed <= 0:
        return Refusal(
            clause="S5.1",
            reason=("The entitlement service assesses nothing payable for %s. Compensation "
                    "status is %s." % (", ".join(pids), ent.get("compensation", {}).get("status"))),
            recommendation=("Tell the passenger no compensation is payable, stating the "
                            "recorded cause and the arrival delay the conclusion rests on "
                            "(S16 requires both). Duty of care under S4 is unaffected."),
            queue="SUPERVISOR")

    # The tool schema carries no amount, so any amount present is defensive only.
    stated = action.args.get("amount_gbp")
    if stated is not None and round(float(stated), 2) != owed:
        return Refusal(
            clause="S10.2 / S10.4",
            reason=("Proposed amount GBP %.2f does not match the entitlement service figure "
                    "of GBP %.2f. A representative who believes the service is wrong must "
                    "not substitute their own figure." % (float(stated), owed)),
            recommendation="Refer stating both figures and the clause believed misapplied.",
            queue="SUPERVISOR")

    if _prior(ledger, "compensation", dossier.booking_ref):
        return Refusal(
            clause="S12.2 / S15.3",
            reason="A compensation payment has already been made on this booking in this case.",
            recommendation="Do not split or repeat a payment. Refer if more is thought owed.",
            queue="SUPERVISOR")
    return None


def _gate_goodwill(action, dossier, sheet, ledger) -> Refusal | None:
    amount = float(action.args.get("amount_gbp") or 0.0)

    # S11.5 -- a pattern of repeated claims is referred, not paid.
    if "REPEAT_GOODWILL_CLAIMANT" in dossier.customer_flags:
        prior = [a for e in (dossier.customer or {}).get("history", []) or []
                 for a in e.get("actions", []) or [] if a.startswith("goodwill_paid")]
        return Refusal(
            clause="S11.5",
            reason=("Customer is flagged REPEAT_GOODWILL_CLAIMANT with prior payments (%s). "
                    "S11.5 requires the history to be taken into account and a pattern of "
                    "repeated claims to be referred rather than paid." % ", ".join(prior)),
            recommendation="Refer with the history, the amount requested and the amount "
                           "properly payable under S5.",
            queue="SUPERVISOR")

    # S11.4 -- goodwill must not be used to close down a compensation argument.
    ent_status = (dossier.entitlement or {}).get("compensation", {}).get("status")
    if ent_status == "NOT_PAYABLE" and sheet.claimed_facts.delay_minutes is not None:
        return Refusal(
            clause="S11.4",
            reason=("Goodwill is not appropriate merely because a passenger is dissatisfied "
                    "that no compensation is payable. Paying goodwill to close down a "
                    "compensation argument misrepresents the passenger's legal position and "
                    "is prohibited."),
            recommendation=("State plainly why no compensation is payable, citing the recorded "
                            "cause and the arrival delay. If a separate service failure under "
                            "S11.3 exists, refer that on its own merits."),
            queue="SUPERVISOR")

    already = sum(float((e.get("payload") or {}).get("amount_gbp") or 0.0)
                  for e in _prior(ledger, "goodwill", dossier.booking_ref))
    if already + amount > GOODWILL_REP_MAX_GBP:
        return Refusal(
            clause="S11.2 / S12.1 / S12.2",
            reason=("Goodwill of GBP %.2f (GBP %.2f already this case) exceeds the GBP %.2f "
                    "representative limit. Limits are per booking and per disruption event and "
                    "may not be circumvented by splitting an amount into several payments."
                    % (amount, already, GOODWILL_REP_MAX_GBP)),
            recommendation=("Refer for authorisation. Supervisor authority covers GBP 151 to "
                            "GBP 1,000; above GBP 1,000 requires a manager."),
            queue="SUPERVISOR")
    return None


def _gate_refund(action, dossier, sheet, ledger) -> Refusal | None:
    amount = float(action.args.get("amount_gbp") or 0.0)
    already = sum(float((e.get("payload") or {}).get("amount_gbp") or 0.0)
                  for e in _prior(ledger, "refund", dossier.booking_ref))
    if already + amount > REFUND_REP_MAX_GBP:
        return Refusal(
            clause="S12.1 / S12.2",
            reason="Refund of GBP %.2f exceeds the GBP %.2f representative limit."
                   % (amount + already, REFUND_REP_MAX_GBP),
            recommendation="Refer for supervisor authorisation.",
            queue="SUPERVISOR")
    return None


def _gate_rebooking(action, dossier, sheet, ledger) -> Refusal | None:
    fare = float(action.args.get("fare_gbp") or 0.0)
    operated_by = (action.args.get("operated_by") or "Aerlink")
    cabin = (action.args.get("cabin") or "").upper()
    pids = action.args.get("passenger_ids") or []

    # S8.2 -- no partner re-routing may be actioned automatically, at any price.
    if operated_by.lower() != "aerlink":
        return Refusal(
            clause="S8.2 / S12.1",
            reason=("Re-routing onto partner carrier %s at GBP %.2f per passenger. No partner "
                    "re-routing may be actioned automatically." % (operated_by, fare)),
            recommendation=("Refer for authorisation -- supervisor at GBP 600 or less per "
                            "passenger, manager above. S8.3 requires the own-carrier options "
                            "considered and why they were unsuitable to be recorded with it."),
            queue="SUPERVISOR")

    # S6.3 / S12.1 -- a representative may action own-carrier, same cabin, no fare difference.
    if fare != 0.0:
        return Refusal(
            clause="S12.1",
            reason="Re-routing carries a fare difference of GBP %.2f." % fare,
            recommendation="Refer for supervisor authorisation.",
            queue="SUPERVISOR")

    booked_cabins = {(dossier.passenger(p) or {}).get("cabin_booked")
                     for p in pids if dossier.passenger(p)}
    if cabin and booked_cabins and cabin not in {c.upper() for c in booked_cabins if c}:
        return Refusal(
            clause="S6.2 / S12.1",
            reason="Re-routing into %s but the passenger(s) booked %s."
                   % (cabin, "/".join(sorted(c for c in booked_cabins if c))),
            recommendation="Refer for supervisor authorisation; a cabin change is outside "
                           "representative authority, and a downgrade engages S9.",
            queue="SUPERVISOR")

    # S6.4 -- do not confirm a seat the passenger has not asked for.
    if not (sheet.asked_for_earliest or sheet.expressed_travel_preference):
        return Refusal(
            clause="S6.4",
            reason=("The passenger has not expressed a preference and has not asked to be "
                    "placed on the earliest available service. Confirming a seat consumes "
                    "inventory and, on a non-refundable fare, incurs cost that cannot be "
                    "recovered."),
            recommendation=("Present the options and ask which the passenger wants. S6.1 gives "
                            "them a choice between earliest re-routing, later re-routing at "
                            "their convenience, and a refund -- the choice belongs to them."),
            queue="SUPERVISOR")

    # S14.4 -- a booked assistance service must be re-booked before the re-routing is confirmed.
    needs_assist = [p for p in pids
                    if (dossier.passenger(p) or {}).get("assistance")]
    if needs_assist:
        raised = any(e.get("action") == "escalation"
                     and (e.get("payload") or {}).get("queue") == "SPECIAL_ASSISTANCE"
                     for e in ledger)
        if not raised:
            return Refusal(
                clause="S14.4",
                reason=("Passenger(s) %s have a declared assistance requirement (%s). "
                        "Confirming a re-routing without re-booking the assistance leaves "
                        "them without support at an airport they did not plan to be in."
                        % (", ".join(needs_assist),
                           ", ".join(sorted({(dossier.passenger(p) or {}).get("assistance")
                                             for p in needs_assist})))),
                recommendation=("Raise a Special Assistance referral to re-book the service on "
                                "the new itinerary first, then confirm the re-routing."),
                queue="SPECIAL_ASSISTANCE")

    if _prior(ledger, "rebooking", dossier.booking_ref):
        return Refusal(
            clause="S15.3",
            reason="A re-booking has already been confirmed on this booking in this case.",
            recommendation="Two re-routings is a serious error. Refer if a change is needed.",
            queue="SUPERVISOR")
    return None


def _gate_hotel(action, dossier, sheet, ledger) -> Refusal | None:
    """S4.5 -- allocation must be CHECKED before a room is promised, and S16 prohibits
    issuing a voucher at a station whose allocation is exhausted."""
    alloc = action.args.get("_allocation")
    if alloc is None:
        return Refusal(
            clause="S4.5",
            reason="Station allocation was not checked before issuing a hotel voucher. "
                   "Representatives must check remaining allocation before promising a room.",
            recommendation="Check GET /stations/{iata}/hotel-allocation for that night, then "
                           "re-attempt or refer.",
            queue="SUPERVISOR")

    remaining = alloc.get("rooms_remaining", 0)
    if remaining <= 0:
        return Refusal(
            clause="S4.5 / S16",
            reason=("The Aerlink allocation at %s for %s is exhausted (%d rooms remaining). "
                    "Issuing a voucher there is prohibited."
                    % (action.args.get("station"), action.args.get("night"), remaining)),
            recommendation=("Refer to Accommodation Services to source additional rooms or "
                            "authorise a passenger-arranged booking reimbursed under S4.4."),
            queue="SUPERVISOR")

    rate = float(alloc.get("rate_gbp") or 0.0)
    if rate > HOTEL_RATE_CAP_GBP:
        return Refusal(
            clause="S4.2 / S12.1",
            reason="Room rate GBP %.2f exceeds the GBP %.2f per room per night cap."
                   % (rate, HOTEL_RATE_CAP_GBP),
            recommendation="Refer for supervisor authorisation to exceed the cap.",
            queue="SUPERVISOR")

    nights = len(_prior(ledger, "hotel_voucher", dossier.booking_ref)) + 1
    if nights > HOTEL_MAX_NIGHTS:
        return Refusal(
            clause="S4.2",
            reason="This would be night %d; the maximum without referral is %d."
                   % (nights, HOTEL_MAX_NIGHTS),
            recommendation="Refer for authorisation beyond three nights.",
            queue="SUPERVISOR")
    return None
