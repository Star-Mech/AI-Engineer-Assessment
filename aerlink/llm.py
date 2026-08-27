"""The OpenAI layer: one structured-output call, one bounded tool loop.

Deliberately hand-rolled rather than built on an agent framework. The loop is about forty
lines, and every tool call has to be intercepted and routed through `policy.gate()` anyway -
in a hand-rolled loop the interception point *is* the loop, which is one obvious place to
look when someone asks where the safety boundary is. Token accounting also stays exact,
which requirement 5 needs.

The write tools here are real tools. The security boundary is not that the model lacks a
binding; it is that `do()` -- which is `work.execute`, and the only path to a POST -- gates
every one of them. Note that `pay_compensation` carries no amount parameter at all: the
figure is read from the entitlement service at execution time, so the model is
schema-level incapable of writing a number into a payment (S10.2).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from openai import OpenAI

from .models import ActionRequest, ContactSheet, Dossier
from .ops import OpsError
from .prompts import AGENT_SYSTEM, EXTRACT_SYSTEM, agent_user, extract_user
from .trace import TRACE

MAX_TURNS = 14
AVAILABILITY_SHOWN = 8

# Per million tokens. Verify against your own account before quoting these; they are here so
# a run reports a number rather than leaving cost as an exercise for the reader.
PRICES_USD = {
    "gpt-5":      {"in": 1.25, "cached_in": 0.125, "out": 10.00},
    "gpt-5-mini": {"in": 0.25, "cached_in": 0.025, "out": 2.00},
}


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    by_model: dict[str, dict] = field(default_factory=dict)

    def add(self, model: str, resp: Any) -> None:
        u = getattr(resp, "usage", None)
        if u is None:
            return
        pt, ct = u.prompt_tokens or 0, u.completion_tokens or 0
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        reasoning = getattr(getattr(u, "completion_tokens_details", None),
                            "reasoning_tokens", 0) or 0
        self.calls += 1
        self.prompt_tokens += pt
        self.cached_tokens += cached
        self.completion_tokens += ct
        self.reasoning_tokens += reasoning
        m = self.by_model.setdefault(model, {"calls": 0, "in": 0, "cached_in": 0, "out": 0})
        m["calls"] += 1
        m["in"] += pt - cached
        m["cached_in"] += cached
        m["out"] += ct

    @property
    def cost_usd(self) -> float:
        total = 0.0
        for model, m in self.by_model.items():
            p = PRICES_USD.get(model) or PRICES_USD.get(model.rsplit("-", 1)[0])
            if not p:
                continue
            total += (m["in"] * p["in"] + m["cached_in"] * p["cached_in"]
                      + m["out"] * p["out"]) / 1_000_000
        return round(total, 6)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "cached_prompt_tokens": self.cached_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "by_model": self.by_model,
            "estimated_cost_usd": self.cost_usd,
        }


def _client() -> OpenAI:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit(
            "OPENAI_API_KEY is not set. Check .env -- the line may still be commented out.")
    # Accept-Encoding is pinned to gzip because some httpx builds ship a zstd decoder that
    # raises "process() takes no keyword arguments" on every response. Harmless either way.
    return OpenAI(api_key=key, default_headers={"Accept-Encoding": "gzip"})


# --------------------------------------------------------------------------
# 1. Extraction
# --------------------------------------------------------------------------

def extract_contact_sheet(case, model: str) -> tuple[ContactSheet, dict]:
    usage = Usage()
    resp = _client().chat.completions.parse(
        model=model,
        messages=[{"role": "system", "content": EXTRACT_SYSTEM},
                  {"role": "user", "content": extract_user(case.meta, case.inbound)}],
        response_format=ContactSheet,
    )
    usage.add(model, resp)
    TRACE.emit("llm", "extract complete  %s  in=%d out=%d  $%.5f"
               % (model, usage.prompt_tokens, usage.completion_tokens, usage.cost_usd))
    return resp.choices[0].message.parsed, usage.as_dict()


# --------------------------------------------------------------------------
# 2. Tools
# --------------------------------------------------------------------------

def _tool(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False}}}


RATIONALE = {"type": "string", "description":
             "why this action, citing the policy clause it rests on. Goes in the case record."}

TOOLS = [
    # ---- read: free, and change nothing -------------------------------------
    _tool("search_availability",
          "Own-carrier seat inventory for a route and date. Returns only options with seats "
          "left. Use before proposing any re-routing.",
          {"from_iata": {"type": "string"}, "to_iata": {"type": "string"},
           "date": {"type": "string", "description": "YYYY-MM-DD"},
           "after": {"type": ["string", "null"], "description": "HH:MM local, or null"}},
          ["from_iata", "to_iata", "date", "after"]),

    _tool("search_partner_availability",
          "Partner-carrier inventory. S8.1 limits when a partner may be used at all, and "
          "S8.2 means no partner re-routing is ever actioned automatically - so this is for "
          "building the comparison S8.3 requires, and the booking itself will be referred.",
          {"from_iata": {"type": "string"}, "to_iata": {"type": "string"},
           "date": {"type": "string"}},
          ["from_iata", "to_iata", "date"]),

    _tool("check_hotel_allocation",
          "Rooms left in Aerlink's contracted allocation at a station for one night. S4.5 "
          "requires checking this before promising a passenger a room.",
          {"station": {"type": "string"}, "night": {"type": "string"}},
          ["station", "night"]),

    _tool("get_flight", "The operational record for one flight on one date.",
          {"flight_no": {"type": "string"}, "date": {"type": "string"}},
          ["flight_no", "date"]),

    _tool("get_disruption_feed",
          "Network advisories and flight events across the whole network for the current window.",
          {}, []),

    _tool("policy_section",
          "Full text of policy sections by number, e.g. ['9.4','5.4']. Prefer this over "
          "policy_search: it is exact.",
          {"sections": {"type": "array", "items": {"type": "string"}}}, ["sections"]),

    _tool("policy_search",
          "Keyword search over the policy. Lexical AND matching only: EVERY term must appear "
          "literally in a section, there is no stemming and no synonyms, and adding terms "
          "narrows the results. Use single broad terms and the document's own vocabulary - "
          "'referral' not 'escalate', 'mobility device' not 'wheelchair'.",
          {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query", "limit"]),

    # ---- write: real, and gated ---------------------------------------------
    _tool("rebook",
          "Confirm seats and issue a new itinerary. Spends money and changes the passenger's "
          "journey. S6.4: do not confirm unless the passenger has expressed a preference or "
          "asked for the earliest service.",
          {"passenger_ids": {"type": "array", "items": {"type": "string"}},
           "option_id": {"type": "string"}, "flight_no": {"type": "string"},
           "date": {"type": "string"}, "cabin": {"type": "string"},
           "fare_gbp": {"type": "number"},
           "operated_by": {"type": "string", "description": "'Aerlink' or the partner name"},
           "rationale": RATIONALE},
          ["passenger_ids", "option_id", "flight_no", "date", "cabin", "fare_gbp",
           "operated_by", "rationale"]),

    _tool("issue_hotel_voucher",
          "Issue a hotel voucher against the station allocation. Spends money.",
          {"station": {"type": "string"}, "night": {"type": "string"},
           "passenger_ids": {"type": "array", "items": {"type": "string"}},
           "rationale": RATIONALE},
          ["station", "night", "passenger_ids", "rationale"]),

    _tool("pay_compensation",
          "Pay what the entitlement calculation service assessed for these passengers. "
          "There is deliberately NO amount parameter: S10.2 makes the service's figure the "
          "amount owed, and it is read at execution time. Do not compute it.",
          {"passenger_ids": {"type": "array", "items": {"type": "string"}},
           "reason": {"type": "string"}, "rationale": RATIONALE},
          ["passenger_ids", "reason", "rationale"]),

    _tool("pay_goodwill",
          "Discretionary payment acknowledging a service failure IN ADDITION TO the "
          "disruption (S11.3). Not for dissatisfaction with the compensation outcome - "
          "S11.4 prohibits that. Representative authority is limited; above it, refer.",
          {"amount_gbp": {"type": "number"}, "reason": {"type": "string"},
           "rationale": RATIONALE},
          ["amount_gbp", "reason", "rationale"]),

    _tool("refund", "Refund all or part of a booking. Spends money.",
          {"passenger_ids": {"type": "array", "items": {"type": "string"}},
           "amount_gbp": {"type": "number"}, "reason": {"type": "string"},
           "rationale": RATIONALE},
          ["passenger_ids", "amount_gbp", "reason", "rationale"]),

    _tool("escalate",
          "Hand a decision to a human. Always permitted. S12.5: record what the passenger "
          "asked for, what the record shows, what you believe the right outcome is, what "
          "blocks you, and what the human must decide. A referral without a recommendation "
          "is incomplete.",
          {"summary": {"type": "string"}, "requested_decision": {"type": "string"},
           "queue": {"type": "string", "description":
                     "SUPERVISOR, YTP, SPECIAL_ASSISTANCE, OPS_LIAISON, CUSTOMER_CONDUCT, "
                     "LOST_PROPERTY or GENERAL"},
           "recommendation": {"type": "string"},
           "blocking_clause": {"type": "string"}},
          ["summary", "requested_decision", "queue", "recommendation", "blocking_clause"]),

    _tool("finish",
          "Call when the case is worked. Ends the loop. NOTE: this system does not send a "
          "reply to the passenger, so anything they must be told or asked has to be captured "
          "in the fields below or it reaches nobody.",
          {"summary": {"type": "string",
                       "description": "what you did and why, in a few lines. Internal."},
           "passenger_must_be_told": {
               "type": "array", "items": {"type": "string"},
               "description": "each thing the passenger has to be told, as a separate line. "
                              "Include anything the policy obliges you to state -- the "
                              "recorded cause and arrival delay behind a refusal (S16), that "
                              "the record shows a different cause than they described "
                              "(S3.1), that care is an entitlement (S4.3)."},
           "awaiting_passenger_decision": {
               "type": "array", "items": {"type": "string"},
               "description": "anything you did NOT action because the choice belongs to the "
                              "passenger under S6.1 and they have not made it. Say exactly "
                              "what they must choose between, including option ids, so a "
                              "human can put it to them without re-working the case. If you "
                              "leave a request unactioned and unrecorded here, it is simply "
                              "dropped."},
           "uncertainties": {"type": "array", "items": {"type": "string"},
                             "description": "anything you remain unsure about (S15.7)"}},
          ["summary", "passenger_must_be_told", "awaiting_passenger_decision",
           "uncertainties"]),
]

WRITE_TOOLS = {"rebook": "rebooking", "issue_hotel_voucher": "hotel_voucher",
               "pay_compensation": "compensation", "pay_goodwill": "goodwill",
               "refund": "refund", "escalate": "escalation"}


def _trim_availability(payload: dict) -> dict:
    """Only bookable options, and only a handful, to keep the context affordable."""
    rows = [r for r in payload.get("results", []) if (r.get("seats_available") or 0) > 0]
    rows.sort(key=lambda r: r.get("arrival_delay_vs_original_minutes", 10**6))
    return {
        "total_results": payload.get("total_results"),
        "bookable_on_this_page": len(rows),
        "note": "Filtered to options with seats available. fare_gbp 0.00 on own carrier in "
                "the same cabin is actionable without referral (S6.3).",
        "options": rows[:AVAILABILITY_SHOWN],
    }


def _run_read_tool(name: str, args: dict, ops, policy) -> Any:
    try:
        if name == "search_availability":
            return _trim_availability(ops.availability(
                args["from_iata"], args["to_iata"], args["date"],
                after=args.get("after"), page_size=60))
        if name == "search_partner_availability":
            return _trim_availability(ops.availability(
                args["from_iata"], args["to_iata"], args["date"],
                page_size=30, partners=True))
        if name == "check_hotel_allocation":
            return ops.hotel_allocation(args["station"], args["night"])
        if name == "get_flight":
            return ops.get_flight(args["flight_no"], args["date"])
        if name == "get_disruption_feed":
            return ops.disruption_feed()
        if name == "policy_section":
            return {"text": policy.section(args["sections"])}
        if name == "policy_search":
            return ops.policy_search(args["query"], min(int(args.get("limit") or 5), 20))
    except OpsError as exc:
        return {"error": exc.code, "message": exc.message,
                "note": "The read failed. Work with what you have, or refer."}
    return {"error": "unknown_tool", "message": name}


# --------------------------------------------------------------------------
# 3. The loop
# --------------------------------------------------------------------------

def run_agent(case, sheet: ContactSheet, dossier: Dossier, policy, ops,
              do: Callable[[ActionRequest], dict], model: str,
              max_turns: int = MAX_TURNS) -> tuple[list[dict], list[ActionRequest], dict]:
    """Work the case. Returns (outcomes, actions attempted, usage).

    `do` gates and performs a write, returning the outcome. A refusal comes back to the
    model as the tool result, so it can adapt rather than simply failing.
    """
    client = _client()
    usage = Usage()
    messages = [{"role": "system", "content": AGENT_SYSTEM},
                {"role": "user", "content": agent_user(case, sheet, dossier, policy)}]

    outcomes: list[dict] = []
    actions: list[ActionRequest] = []
    final: dict = {}

    for turn in range(max_turns):
        TRACE.emit("llm", "turn %d/%d requesting..." % (turn + 1, max_turns))
        before_in, before_out = usage.prompt_tokens, usage.completion_tokens
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS, tool_choice="auto")
        usage.add(model, resp)
        TRACE.turn(turn + 1, model, usage.prompt_tokens - before_in,
                   usage.completion_tokens - before_out, usage.cached_tokens)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            final = {"summary": msg.content or "", "uncertainties": []}
            break

        stop = False
        for call in msg.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            TRACE.tool(name, args)
            if name == "finish":
                final = {"summary": args.get("summary", ""),
                         "passenger_must_be_told": args.get("passenger_must_be_told") or [],
                         "awaiting_passenger_decision":
                             args.get("awaiting_passenger_decision") or [],
                         "uncertainties": args.get("uncertainties", []) or []}
                result: Any = {"ok": True}
                stop = True
            elif name in WRITE_TOOLS:
                action = ActionRequest(kind=WRITE_TOOLS[name],
                                       args={k: v for k, v in args.items() if k != "rationale"},
                                       rationale=args.get("rationale", ""))
                actions.append(action)
                outcome = do(action)
                outcomes.append(outcome)
                result = _tool_result_for(outcome)
            else:
                result = _run_read_tool(name, args, ops, policy)

            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result, ensure_ascii=False, default=str)})
        if stop:
            break
    else:
        final = {"summary": "Turn limit reached before the agent called finish.",
                 "uncertainties": ["The case was cut short at the %d-turn limit; treat the "
                                   "record as incomplete." % max_turns]}

    return outcomes, actions, {**usage.as_dict(), "agent_final": final,
                               "turns_used": min(turn + 1, max_turns)}


def _tool_result_for(outcome: dict) -> dict:
    """What the model sees back. A refusal is informative, not just a failure."""
    if outcome["outcome"] == "REFUSED":
        return {
            "status": "REFUSED_BY_AUTHORITY_GATE",
            "blocking_clause": outcome["clause"],
            "reason": outcome["reason"],
            "referral_raised_to": outcome["queue"],
            "note": "A referral carrying this clause and a recommendation has already been "
                    "raised on your behalf. Do not retry this action, do not reword it, and "
                    "do not split it into smaller amounts (S12.2). Continue with the rest of "
                    "the case.",
        }
    if outcome["outcome"] == "FAILED":
        transport = outcome.get("error") == "connection_error"
        return {
            "status": "FAILED",
            "error": outcome.get("error"),
            "message": outcome.get("message"),
            "note": (
                "This write failed in transport, so whether the operations API processed it "
                "is UNKNOWN. Do NOT reissue it -- if it did land, reissuing produces a "
                "duplicate remedy, which S15.3 calls a serious error. Escalate instead, "
                "stating that the action may or may not have been applied and asking a human "
                "to verify against the audit trail."
                if transport else
                "The operations API rejected this. Do not reword and retry. If the action is "
                "still the right one, escalate it for a human to action."),
        }
    return {"status": "DONE", "result": outcome.get("result")}
