#!/usr/bin/env python3
"""Aerlink disruption desk.

    python run.py --case cases/case-08          # one case, dry run
    python run.py --all                         # all twelve, dry run
    python run.py --all --execute --yes         # the real thing

Two safety defaults, both deliberate:

  * **Dry run unless --execute.** Writes are simulated and GET /_audit is left untouched,
    so a development run cannot pollute the graded audit trail.
  * **--yes required for anything that calls OpenAI.** The key has a hard spending cap and
    a full sweep is not free. --offline replays recorded fixtures and never calls out at
    all, which is how the pipeline is developed and tested.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from aerlink.models import ActionRequest, ContactSheet
from aerlink.ops import OpsClient
from aerlink.policy import advisory_referrals, hard_bars, verify_limits_against
from aerlink.policydoc import PolicyDoc
from aerlink.trace import TRACE
from aerlink.work import (
    Case, build_dossier, build_record, execute, load_case, raise_referral,
)

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------

def load_fixture_sheet(case_id: str) -> ContactSheet:
    path = FIXTURES / "contact_sheets" / ("%s.json" % case_id)
    if not path.exists():
        raise SystemExit(
            "No offline fixture for %s (%s).\n"
            "Record one with:  python run.py --case cases/%s --yes --record-fixtures"
            % (case_id, path, case_id))
    return ContactSheet.model_validate_json(path.read_text(encoding="utf-8"))


def load_fixture_plan(case_id: str) -> list[ActionRequest]:
    path = FIXTURES / "plans" / ("%s.json" % case_id)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [ActionRequest(kind=a["kind"], args=a.get("args", {}),
                          rationale=a.get("rationale", "")) for a in raw]


def save_fixtures(case_id: str, sheet: ContactSheet, actions: list[ActionRequest]) -> None:
    (FIXTURES / "contact_sheets").mkdir(parents=True, exist_ok=True)
    (FIXTURES / "plans").mkdir(parents=True, exist_ok=True)
    (FIXTURES / "contact_sheets" / ("%s.json" % case_id)).write_text(
        sheet.model_dump_json(indent=2), encoding="utf-8")
    (FIXTURES / "plans" / ("%s.json" % case_id)).write_text(
        json.dumps([{"kind": a.kind, "args": a.args, "rationale": a.rationale}
                    for a in actions], indent=2), encoding="utf-8")


# --------------------------------------------------------------------------

def work_case(case: Case, ops: OpsClient, policy: PolicyDoc, args) -> dict:
    """One case, end to end."""
    t0 = time.time()

    # 1. Read the message. Everything this produces is a claim.
    TRACE.phase("extract", "offline fixture" if args.offline else args.model_extract)
    if args.offline:
        sheet = load_fixture_sheet(case.case_id)
        extract_usage = {"offline": True}
    else:
        from aerlink.llm import extract_contact_sheet
        sheet, extract_usage = extract_contact_sheet(case, model=args.model_extract)
    # Always the same shape. A case barred at identity never reaches the agent, and an
    # earlier version left `usage` as the bare extract dict in that case -- which the spend
    # total then skipped entirely, under-reporting every refused case as free.
    usage = {"extract": extract_usage, "agent": None}

    # 2 & 3. Identity, then the four authoritative fetches.
    TRACE.phase("identity + dossier")
    dossier = build_dossier(ops, sheet, case.meta)
    TRACE.emit("info", "identity %s  %s"
               % ("CONFIRMED " + str(dossier.booking_ref) if dossier.identity.confirmed
                  else "NOT CONFIRMED", dossier.identity.method or dossier.identity.reason[:70]))

    outcomes: list[dict] = []

    # 4. Hard bars, before anything is planned.
    TRACE.phase("hard bars")
    bars = hard_bars(dossier, sheet)
    if bars:
        TRACE.emit("gate", "BARRED by %s -- the agent will not be run"
                   % ", ".join(b.clause for b in bars))
    for bar in bars:
        outcomes.append(raise_referral(
            ops, bar, dossier,
            summary="%s: %s" % (case.case_id, case.meta.get("subject", ""))))

    # Referrals that must be raised but do not stop the case being worked.
    for ref in advisory_referrals(dossier, sheet):
        outcomes.append(raise_referral(
            ops, ref, dossier,
            summary="%s: %s" % (case.case_id, case.meta.get("subject", ""))))

    # 5 & 6. Work the case. Every write goes through the gate on its way out, and the
    # gate's verdict goes back to the agent so a refusal can be adapted to.
    actions: list[ActionRequest] = []
    agent_final: dict = {}
    if not bars:
        if args.offline:
            actions = load_fixture_plan(case.case_id)
            for action in actions:
                outcomes.append(execute(ops, action, dossier, sheet))
        else:
            TRACE.phase("agent", args.model_agent)
            from aerlink.llm import run_agent
            agent_outcomes, actions, agent_usage = run_agent(
                case, sheet, dossier, policy, ops,
                do=lambda a: execute(ops, a, dossier, sheet),
                model=args.model_agent)
            outcomes.extend(agent_outcomes)
            agent_final = agent_usage.pop("agent_final", {})
            usage["agent"] = agent_usage

    if args.record_fixtures and not args.offline:
        save_fixtures(case.case_id, sheet, actions)

    # 7. The record.
    TRACE.phase("record")
    record = build_record(case, sheet, dossier, outcomes, ops,
                          extra={"usage": usage, "elapsed_s": round(time.time() - t0, 2)})
    if agent_final:
        record["reasoning"]["agent_summary"] = agent_final.get("summary")
        record["uncertainties"] = (record.get("uncertainties", [])
                                   + list(agent_final.get("uncertainties", [])))
        # Without these two, an option presented but not booked reaches nobody.
        record["communication_obligations"] += list(
            agent_final.get("passenger_must_be_told", []))
        for item in agent_final.get("awaiting_passenger_decision", []):
            record["human_still_needs_to"].append({
                "queue": "CASE_HANDLER",
                "clause": "S6.1 / S15.1",
                "raised_by": "case handler",
                "summary": "Awaiting the passenger's election; the choice is theirs to make.",
                "decide": item,
                "recommendation": "Put this to the passenger and action their answer.",
            })
    return record


def summarise(record: dict) -> str:
    d = record["decision"]
    done = [a for a in record["actions_taken"] if a["outcome"] == "DONE"]
    refused = record["actions_refused"]
    esc = record["human_still_needs_to"]
    ident = "confirmed %s" % d["booking_ref"] if d["identity"]["confirmed"] else \
            "NOT CONFIRMED (%d candidates)" % len(d["identity"]["candidates_seen"])
    return "  %-9s %-34s done=%d refused=%d escalations=%d" % (
        record["case_id"], ident, len(done), len(refused), len(esc))


def main() -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description="Work Aerlink disruption cases.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--case", help="path to a case directory (inbound.txt + meta.json)")
    src.add_argument("--cases", nargs="+", metavar="ID",
                     help="a subset, by id or path, e.g. --cases case-07 case-09 case-11. "
                          "Note that cases 09 and 11 contend for the same hotel room, so "
                          "separating them across a reset makes that contention untestable.")
    src.add_argument("--all", action="store_true", help="every case under cases/")

    ap.add_argument("--execute", action="store_true",
                    help="issue real writes. Without this, writes are simulated.")
    ap.add_argument("--offline", action="store_true",
                    help="replay recorded fixtures; makes no OpenAI calls at all")
    ap.add_argument("--yes", action="store_true",
                    help="required to permit any run that calls OpenAI")
    ap.add_argument("--record-fixtures", action="store_true",
                    help="save this run's extraction and plan for later offline replay")
    ap.add_argument("--reset", action="store_true", help="POST /_reset before starting")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress the live trace on stderr. trace.jsonl is written either way.")
    ap.add_argument("--out", default=str(ROOT / "runs"))
    ap.add_argument("--model-extract", default=os.environ.get("MODEL_EXTRACT", "gpt-5-mini"))
    ap.add_argument("--model-agent", default=os.environ.get("MODEL_AGENT", "gpt-5"))
    args = ap.parse_args()

    if not args.offline and not args.yes:
        print("This run would call OpenAI, which costs money against a capped key.\n"
              "Re-run with --yes to confirm, or with --offline to replay fixtures for free.",
              file=sys.stderr)
        return 2

    ops = OpsClient(os.environ.get("OPS_BASE_URL", "http://127.0.0.1:8642"),
                    os.environ.get("OPS_API_KEY", "aerlink-ops-local-key"),
                    dry_run=not args.execute)
    try:
        ops.health()
    except Exception as exc:
        print("The operations API is not reachable: %s\nStart it with: "
              "python env/ops_server.py" % exc, file=sys.stderr)
        return 1

    if args.reset:
        ops.reset()
        print("ops state reset")

    policy = PolicyDoc.fetch(ops)
    for warning in verify_limits_against(policy.content):
        print("POLICY DRIFT: %s" % warning, file=sys.stderr)

    if args.all:
        cases = [load_case(p) for p in sorted((ROOT / "cases").iterdir()) if p.is_dir()]
    elif args.cases:
        cases = [load_case(c if Path(c).exists() else ROOT / "cases" / c) for c in args.cases]
    else:
        cases = [load_case(args.case)]

    out_dir = Path(args.out) / _stamp()
    out_dir.mkdir(parents=True, exist_ok=True)
    TRACE.start(enabled=not args.quiet, path=out_dir / "trace.jsonl")

    mode = "EXECUTE" if args.execute else "dry-run"
    print("\n%s | %s | %d case(s) | policy %s v%s (%d sections)"
          % (mode, "offline" if args.offline else "live", len(cases),
             policy.document_ref, policy.version, len(policy.sections)))
    print("-" * 78)

    records = []
    for case in cases:
        # Each case gets a fresh trail so the record shows only its own consultations.
        ops.consulted, ops.ledger = [], []
        TRACE.case(case.case_id)
        try:
            record = work_case(case, ops, policy, args)
        except Exception as exc:  # a failed case must not take the sweep down
            record = {"case_id": case.case_id, "error": "%s: %s" % (type(exc).__name__, exc),
                      "decision": {"booking_ref": None,
                                   "identity": {"confirmed": False, "candidates_seen": []}},
                      "actions_taken": [], "actions_refused": [], "human_still_needs_to": []}
            print("  %-9s ERROR %s" % (case.case_id, exc))
        else:
            print(summarise(record))
        records.append(record)
        (out_dir / ("%s.json" % case.case_id)).write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    # Snapshot the service's own permanent log alongside the records. Batched runs reset
    # between batches, so /_audit itself only ever holds the most recent batch.
    audit = ops.audit()
    (out_dir / "_audit.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")

    spend = 0.0
    for r in records:
        u = r.get("usage") or {}
        for stage in ("extract", "agent"):
            spend += (u.get(stage) or {}).get("estimated_cost_usd", 0.0) or 0.0

    summary = {
        "at": _stamp(), "mode": mode, "offline": args.offline,
        "cases": len(records),
        "audit_totals": audit["totals"],
        "estimated_spend_usd": round(spend, 4),
        "records": [r["case_id"] for r in records],
    }
    (out_dir / "_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if spend:
        print("estimated spend this run: $%.4f" % spend)

    print("-" * 78)
    print("money moved: GBP %.2f | rebookings %d | vouchers %d | escalations %d"
          % (audit["totals"]["money_paid_gbp"], audit["totals"]["rebookings_confirmed"],
             audit["totals"]["hotel_vouchers_issued"], audit["totals"]["escalations_raised"]))
    print("records: %s" % out_dir)
    print("trace  : %s" % (out_dir / "trace.jsonl"))
    TRACE.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
