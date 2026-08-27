"""HTTP client for the Aerlink operations API.

Three responsibilities beyond issuing requests:

1. **Survive the service.** The availability search fails the first call to every distinct
   query and every seventh thereafter (`ops_server.py:_should_fail_flaky`), and the whole
   service rate-limits at 30 requests / 10 seconds. Both are handled here so that no prompt
   ever has to reason about a 503.

2. **Keep the consulted trail.** Every call appends to ``consulted``. Requirement 3's "what
   was consulted", and policy S15.7's "the operational facts relied on and where they came
   from", then fall out of the client rather than being assembled by hand later.

3. **Hold the dry-run boundary.** With ``dry_run=True`` no POST reaches the network. Writes
   are simulated, recorded in ``ledger`` exactly as real ones are, and ``GET /_audit`` is
   left untouched, so a development run cannot pollute the graded audit trail.
"""

from __future__ import annotations

import itertools
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

from .trace import TRACE

# The server allows 30 per 10s. Stay under it rather than relying on the retry path.
RATE_LIMIT_REQUESTS = 28
RATE_LIMIT_WINDOW_S = 10.0

# The first attempt at any distinct availability query always 503s, so one retry is the
# minimum that can ever work. Four gives headroom for the every-seventh failure landing
# on a retry.
MAX_ATTEMPTS = 4
BACKOFF_S = (0.4, 1.2, 2.5)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OpsError(Exception):
    """A request the operations API refused, after retries."""

    def __init__(self, status: int, code: str, message: str, path: str):
        self.status = status
        self.code = code
        self.message = message
        self.path = path
        super().__init__("%s %s -> %s: %s" % (status, path, code, message))


class OpsClient:
    def __init__(self, base_url: str, api_key: str, *,
                 dry_run: bool = True, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dry_run = dry_run
        self.timeout = timeout

        self.consulted: list[dict[str, Any]] = []
        self.ledger: list[dict[str, Any]] = []

        self._session = requests.Session()
        self._session.headers.update({"X-Ops-Key": api_key})
        # No keep-alive. An agent turn can sit for 30s between calls, by which time a pooled
        # socket to the local server has gone stale and the next request dies with
        # ConnectionResetError. On a GET that is a retry; on a money-moving POST it is a
        # write whose fate is unknown. Against a server on loopback, a fresh connection per
        # request costs nothing and removes the failure mode entirely.
        self._session.headers.update({"Connection": "close"})
        self._lock = threading.Lock()
        self._times: list[float] = []
        self._sim_seq = itertools.count(1)

    # -- plumbing ---------------------------------------------------------

    def _throttle(self) -> None:
        with self._lock:
            now = time.time()
            self._times = [t for t in self._times if now - t < RATE_LIMIT_WINDOW_S]
            if len(self._times) >= RATE_LIMIT_REQUESTS:
                sleep_for = RATE_LIMIT_WINDOW_S - (now - self._times[0]) + 0.05
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.time()
                self._times = [t for t in self._times if now - t < RATE_LIMIT_WINDOW_S]
            self._times.append(time.time())

    def _record(self, method: str, path: str, params: dict | None,
                status: int | str, *, simulated: bool = False, note: str = "") -> None:
        self.consulted.append({
            "n": len(self.consulted) + 1,
            "at": _now(),
            "method": method,
            "path": path,
            "params": {k: v for k, v in (params or {}).items() if v is not None} or None,
            "status": status,
            "simulated": simulated or None,
            "note": note or None,
        })

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: dict | None = None) -> Any:
        last: OpsError | None = None
        for attempt in range(MAX_ATTEMPTS):
            self._throttle()
            t0 = time.time()
            try:
                resp = self._session.request(
                    method, self.base_url + path,
                    params={k: v for k, v in (params or {}).items() if v is not None},
                    json=json, timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last = OpsError(0, "connection_error", str(exc), path)
                # GETs are idempotent, so retrying costs nothing. POSTs are not: rebooking,
                # payments, refunds and vouchers all move money or inventory, and a transport
                # failure does not tell us whether the server processed the request. Retrying
                # blind is how a passenger gets re-routed twice, which S15.3 calls a serious
                # error. Fail once, loudly, and let the caller verify or refer.
                if method != "GET":
                    self._record(method, path, params, "connection_error",
                                 note="not retried: non-idempotent")
                    TRACE.warn("write failed in transport; NOT retried (fate unknown)",
                               path=path)
                    raise last
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
                    continue
                self._record(method, path, params, "connection_error")
                raise last

            ms = (time.time() - t0) * 1000
            if resp.status_code < 400:
                self._record(method, path, params, resp.status_code)
                TRACE.ops(method, path, resp.status_code, ms)
                return resp.json()

            try:
                body = resp.json()
            except ValueError:
                body = {}
            err = OpsError(resp.status_code, body.get("error", "unknown"),
                           body.get("message", resp.text[:200]), path)

            # 503 is expected on the availability search; 429 means we mis-throttled.
            if resp.status_code in (503, 429) and attempt < MAX_ATTEMPTS - 1:
                self._record(method, path, params, resp.status_code,
                             note="retrying (attempt %d)" % (attempt + 1))
                TRACE.ops(method, path, resp.status_code, ms,
                          "retry %d/%d" % (attempt + 1, MAX_ATTEMPTS - 1))
                time.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
                last = err
                continue

            self._record(method, path, params, resp.status_code, note=err.code)
            TRACE.ops(method, path, resp.status_code, ms, err.code)
            raise err

        assert last is not None
        raise last

    def _write(self, path: str, payload: dict, action: str) -> dict:
        """Issue a write, or simulate it when dry_run is set. Always ledgered."""
        if self.dry_run:
            result = {
                "simulated": True,
                "id": "SIM-%05d" % next(self._sim_seq),
                "status": "SIMULATED",
                **payload,
            }
            self._record("POST", path, None, "simulated", simulated=True)
            TRACE.ops("POST", path, "simulated", 0)
        else:
            result = self._request("POST", path, json=payload)

        self.ledger.append({
            "n": len(self.ledger) + 1,
            "at": _now(),
            "action": action,
            "path": path,
            "payload": payload,
            "simulated": self.dry_run,
            "result": result,
        })
        return result

    # -- reads ------------------------------------------------------------

    def health(self) -> dict:
        return self._request("GET", "/health")

    def search_bookings(self, q: str) -> dict:
        return self._request("GET", "/bookings/search", params={"q": q})

    def get_booking(self, booking_ref: str) -> dict:
        return self._request("GET", "/bookings/%s" % booking_ref.upper())

    def get_flight(self, flight_no: str, date: str) -> dict:
        return self._request("GET", "/flights/%s" % flight_no.upper(), params={"date": date})

    def availability(self, origin: str, destination: str, date: str, *,
                     after: str | None = None, booking_ref: str | None = None,
                     page: int = 1, page_size: int = 20, partners: bool = False) -> dict:
        path = "/flights/availability/partners" if partners else "/flights/availability"
        return self._request("GET", path, params={
            "from": origin.upper(), "to": destination.upper(), "date": date,
            "after": after, "booking_ref": booking_ref,
            "page": page, "page_size": page_size,
        })

    def policy_document(self) -> dict:
        return self._request("GET", "/policy/document")

    def policy_search(self, q: str, limit: int = 5) -> dict:
        return self._request("GET", "/policy/search", params={"q": q, "limit": limit})

    def customer_history(self, customer_id: str) -> dict:
        return self._request("GET", "/customers/%s/history" % customer_id.upper())

    def calculate_entitlement(self, booking_ref: str, passenger_id: str | None = None) -> dict:
        return self._request("GET", "/entitlements/calculate", params={
            "booking_ref": booking_ref.upper(), "passenger_id": passenger_id})

    def hotel_allocation(self, iata: str, night: str) -> dict:
        return self._request("GET", "/stations/%s/hotel-allocation" % iata.upper(),
                             params={"night": night})

    def disruption_feed(self) -> dict:
        return self._request("GET", "/disruption/feed")

    def audit(self) -> dict:
        return self._request("GET", "/_audit")

    # Audit writes, keyed by the ledger `action` name the gate checks against.
    _AUDIT_KINDS = (
        ("rebookings", "rebooking"), ("refunds", "refund"),
        ("hotel_vouchers", "hotel_voucher"), ("escalations", "escalation"),
    )

    def prior_writes(self, booking_ref: str | None) -> list[dict[str, Any]]:
        """Every write the SERVICE already holds against this booking, ledger-shaped.

        The gate's duplicate and aggregate checks originally read only this process's own
        ledger, which quietly made every limit per-session rather than per-booking. S12.2
        says the opposite: limits "are assessed per booking and per disruption event" and
        "may not be circumvented by... paying separate passengers on one booking
        separately". A desk has several representatives working at once, so two sessions
        could each pay GBP 100 of goodwill against a GBP 150 limit and neither would see the
        other. Reading the service's own log closes that.

        /_audit is neither rate limited nor itself audited, so this is cheap.
        """
        out: list[dict[str, Any]] = []
        if not booking_ref:
            return out
        ref = booking_ref.upper()
        try:
            writes = self.audit().get("writes", {})
        except OpsError:
            # Fall back to the local view rather than failing open on the limit checks.
            TRACE.warn("could not read /_audit; limits fall back to this session only")
            return [e for e in self.ledger
                    if (e.get("payload") or {}).get("booking_ref") == ref]

        for key, action in self._AUDIT_KINDS:
            for row in writes.get(key, []) or []:
                if (row.get("booking_ref") or "").upper() == ref:
                    out.append({"action": action, "payload": row, "from_audit": True})
        for row in writes.get("payments", []) or []:
            if (row.get("booking_ref") or "").upper() == ref:
                action = "goodwill" if row.get("type") == "GOODWILL" else "compensation"
                out.append({"action": action, "payload": row, "from_audit": True})

        # In dry run nothing reaches the service, so the local ledger is the only record.
        if self.dry_run:
            out += [e for e in self.ledger
                    if (e.get("payload") or {}).get("booking_ref") == ref]
        return out

    def reset(self) -> dict:
        """Clears every write and restores hotel allocations. Deliberately ignores dry_run:
        resetting is how you get a clean run, not an action taken against a passenger."""
        return self._request("POST", "/_reset", json={})

    # -- writes -----------------------------------------------------------

    def rebook(self, booking_ref: str, passenger_ids: list[str], option_id: str,
               flight_no: str, date: str, cabin: str, fare_gbp: float,
               notes: str | None = None) -> dict:
        return self._write("/rebooking", {
            "booking_ref": booking_ref.upper(), "passenger_ids": passenger_ids,
            "option_id": option_id, "flight_no": flight_no, "date": date,
            "cabin": cabin, "fare_gbp": fare_gbp, "notes": notes,
        }, action="rebooking")

    def issue_hotel_voucher(self, booking_ref: str, station: str, night: str,
                            passenger_ids: list[str], notes: str | None = None) -> dict:
        return self._write("/vouchers/hotel", {
            "booking_ref": booking_ref.upper(), "station": station.upper(),
            "night": night, "passenger_ids": passenger_ids, "notes": notes,
        }, action="hotel_voucher")

    def pay_compensation(self, booking_ref: str, amount_gbp: float,
                         passenger_ids: list[str], reason: str) -> dict:
        return self._write("/payments/compensation", {
            "booking_ref": booking_ref.upper(), "amount_gbp": amount_gbp,
            "passenger_ids": passenger_ids, "reason": reason,
        }, action="compensation")

    def pay_goodwill(self, booking_ref: str, amount_gbp: float, reason: str) -> dict:
        return self._write("/payments/goodwill", {
            "booking_ref": booking_ref.upper(), "amount_gbp": amount_gbp, "reason": reason,
        }, action="goodwill")

    def refund(self, booking_ref: str, passenger_ids: list[str], amount_gbp: float,
               reason: str | None = None) -> dict:
        return self._write("/refunds", {
            "booking_ref": booking_ref.upper(), "passenger_ids": passenger_ids,
            "amount_gbp": amount_gbp, "reason": reason,
        }, action="refund")

    def escalate(self, summary: str, requested_decision: str, *,
                 booking_ref: str | None = None, queue: str = "GENERAL",
                 recommendation: str | None = None,
                 blocking_clause: str | None = None) -> dict:
        return self._write("/escalations", {
            "summary": summary, "requested_decision": requested_decision,
            "booking_ref": booking_ref.upper() if booking_ref else None,
            "queue": queue, "recommendation": recommendation,
            "blocking_clause": blocking_clause,
        }, action="escalation")
