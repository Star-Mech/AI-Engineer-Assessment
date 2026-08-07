#!/usr/bin/env python3
"""
Aerlink Operations API - local mock service.

Standard library only. No installation required.

    python3 ops_server.py

Listens on http://127.0.0.1:8642 by default. Override with OPS_PORT / OPS_HOST.
All endpoints except /health require the header  X-Ops-Key: <key>  where <key> is
OPS_API_KEY from the environment (default: aerlink-ops-local-key).

See API.md for the endpoint reference.
"""

import hashlib
import json
import os
import random
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

HOST = os.environ.get("OPS_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPS_PORT", "8642"))
API_KEY = os.environ.get("OPS_API_KEY", "aerlink-ops-local-key")

RATE_LIMIT_REQUESTS = int(os.environ.get("OPS_RATE_LIMIT", "30"))
RATE_LIMIT_WINDOW_S = 10.0

CABIN_RANK = {"ECONOMY": 0, "PREMIUM": 1, "BUSINESS": 2, "FIRST": 3}

BAND_COMPENSATION_GBP = {"A": 220.0, "B": 350.0, "C": 520.0}
BAND_REDUCTION_THRESHOLD_MIN = {"A": 240, "B": 300, "C": 360}
BAND_CARE_THRESHOLD_MIN = {"A": 120, "B": 180, "C": 240}
BAND_DOWNGRADE_PCT = {"A": 0.30, "B": 0.50, "C": 0.75}

EXTRAORDINARY_CAUSES = {
    "WEATHER", "ATC_RESTRICTION", "ATC_STRIKE", "SECURITY",
    "POLITICAL", "BIRDSTRIKE", "MEDICAL_DIVERSION",
}

# Routes/dates with no own-carrier availability at all.
NO_OWN_AVAILABILITY = {("LHR", "LIS", "2026-08-08"), ("LIS", "LHR", "2026-08-08")}

STOPWORDS = {
    "a", "an", "the", "of", "for", "to", "in", "on", "at", "is", "are", "was", "were",
    "be", "been", "and", "or", "if", "it", "its", "my", "i", "we", "you", "what", "do",
    "does", "did", "can", "could", "should", "would", "how", "when", "who", "whom",
    "this", "that", "these", "those", "with", "from", "by", "as", "about",
}


def _load(name):
    with open(os.path.join(DATA, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


BOOKINGS = _load("bookings.json")
FLIGHTS = _load("flights.json")
CUSTOMERS = _load("customers.json")
STATIONS = _load("stations.json")
FEED = _load("feed.json")

with open(os.path.join(DATA, "policy.md"), "r", encoding="utf-8") as fh:
    POLICY_TEXT = fh.read()


# --------------------------------------------------------------------------
# Mutable state. Everything here is reset by POST /_reset.
# --------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.audit = []
        self.rebookings = {}
        self.payments = {}
        self.vouchers = {}
        self.escalations = {}
        self.refunds = {}
        self.attempt_counts = {}
        self.request_times = []
        self.seq = 0
        self.hotel_remaining = {
            k: v["rooms_remaining"]
            for k, v in STATIONS["hotel_allocation"].items()
        }

    def next_id(self, prefix):
        self.seq += 1
        return "%s-%05d" % (prefix, self.seq)


STATE = State()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Policy chunking, for /policy/search
# --------------------------------------------------------------------------

def _chunk_policy(text):
    chunks = []
    current = {"heading": "Preamble", "level": 0, "lines": []}
    for line in text.splitlines():
        m = re.match(r"^(#{2,3})\s+(.*)$", line)
        if m:
            if current["lines"]:
                chunks.append(current)
            current = {"heading": m.group(2).strip(), "level": len(m.group(1)), "lines": []}
        else:
            current["lines"].append(line)
    if current["lines"]:
        chunks.append(current)

    out = []
    for c in chunks:
        body = "\n".join(c["lines"]).strip()
        if not body:
            continue
        sec = re.match(r"^([\d.]+)\s+(.*)$", c["heading"])
        out.append({
            "section": sec.group(1) if sec else None,
            "heading": c["heading"],
            "text": body,
            "_tokens": set(re.findall(r"[a-z0-9]+", (c["heading"] + " " + body).lower())),
        })
    return out


POLICY_CHUNKS = _chunk_policy(POLICY_TEXT)


def policy_search(query, limit):
    """Lexical AND search. Every non-stopword term must be present in the section.

    No stemming, no synonyms, no fuzzy matching. A section about 'mobility devices'
    is not found by searching for 'wheelchair'.
    """
    terms = [t for t in re.findall(r"[a-z0-9]+", (query or "").lower()) if t not in STOPWORDS]
    if not terms:
        return []
    hits = []
    for chunk in POLICY_CHUNKS:
        if all(t in chunk["_tokens"] for t in terms):
            score = sum(chunk["_tokens"].__contains__(t) for t in terms) / float(len(terms))
            occurrences = sum(len(re.findall(r"\b%s\b" % re.escape(t), chunk["text"].lower())) for t in terms)
            hits.append((occurrences, score, chunk))
    hits.sort(key=lambda h: (-h[0], h[2]["section"] or "zz"))
    return [
        {
            "section": c["section"],
            "heading": c["heading"],
            "text": c["text"],
            "match_count": occ,
        }
        for occ, _s, c in hits[:limit]
    ]


# --------------------------------------------------------------------------
# Availability generation. Deterministic per (origin, destination, date).
# --------------------------------------------------------------------------

PARTNER_CARRIERS = [
    ("IB", "Iberia"), ("AF", "Air France"), ("KL", "KLM"),
    ("LH", "Lufthansa"), ("TP", "TAP Air Portugal"), ("EI", "Aer Lingus"),
]


def _rng(*parts):
    seed = int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)
    return random.Random(seed)


def _distance(origin, destination):
    d = STATIONS["route_distances_km"]
    return d.get("%s-%s" % (origin, destination)) or d.get("%s-%s" % (destination, origin))


def generate_availability(origin, destination, date, partners=False):
    key = (origin, destination, date)
    if not partners and key in NO_OWN_AVAILABILITY:
        return []

    rng = _rng(origin, destination, date, "partners" if partners else "own")
    count = rng.randint(58, 74) if not partners else rng.randint(6, 14)
    distance = _distance(origin, destination) or 1000

    rows = []
    for i in range(count):
        hour = rng.randint(5, 22)
        minute = rng.choice([0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55])
        block = int(distance / 11) + rng.randint(10, 45)
        dep_min = hour * 60 + minute
        arr_min = dep_min + block
        day_offset = arr_min // 1440
        arr_min = arr_min % 1440

        if partners:
            code, name = PARTNER_CARRIERS[rng.randrange(len(PARTNER_CARRIERS))]
            flight_no = "%s%d" % (code, rng.randint(100, 998))
            operated_by = name
            fare = round(rng.uniform(240, 940), 2)
        else:
            flight_no = "AK%d" % rng.randint(100, 998)
            operated_by = "Aerlink"
            fare = round(rng.uniform(0, 320), 2)

        cabin = rng.choice(["ECONOMY"] * 8 + ["PREMIUM", "BUSINESS"])
        rows.append({
            "option_id": "OPT-%s" % hashlib.sha1(
                ("%s%s%s%s%d" % (origin, destination, date, flight_no, i)).encode()
            ).hexdigest()[:10].upper(),
            "flight_no": flight_no,
            "operated_by": operated_by,
            "origin": origin,
            "destination": destination,
            "date": date,
            "departure_local": "%02d:%02d" % (hour, minute),
            "arrival_local": "%02d:%02d%s" % (arr_min // 60, arr_min % 60, "+1" if day_offset else ""),
            "_sort": dep_min,
            "_arrival_abs_min": day_offset * 1440 + arr_min,
            "cabin": cabin,
            "seats_available": rng.randint(0, 9),
            "fare_gbp": fare,
        })

    rows.sort(key=lambda r: r["_sort"])
    return rows


def _scheduled_arrival_minutes(flight):
    ts = flight.get("scheduled_arrival")
    if not ts:
        return None
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    return dt.hour * 60 + dt.minute


# --------------------------------------------------------------------------
# Entitlement calculation. This is the authoritative implementation of
# Sections 3, 4, 5 and 9 of APCP-2026-04.
# --------------------------------------------------------------------------

def band_for(distance_km):
    if distance_km is None:
        return None
    if distance_km <= 1500:
        return "A"
    if distance_km <= 3500:
        return "B"
    return "C"


def calculate_entitlement(booking_ref, passenger_id=None):
    booking = BOOKINGS.get(booking_ref)
    if not booking:
        return None, ("booking_not_found", "No booking with reference %s." % booking_ref)

    disruption = booking.get("disruption")
    if not disruption:
        return {
            "booking_ref": booking_ref,
            "status": "NO_DISRUPTION_RECORDED",
            "note": "The operational record holds no disruption event against this booking. "
                    "Nothing is payable under Sections 4, 5 or 9. If the passenger is claiming "
                    "for something else, this service does not assess it.",
            "passengers": [],
        }, None

    seg_id = disruption.get("affected_segment")
    segment = next((s for s in booking["segments"] if s["segment_id"] == seg_id), None)
    if not segment:
        return None, ("segment_not_found", "Affected segment %s not present on booking." % seg_id)

    fkey = "%s:%s" % (segment["flight_no"], segment["date"])
    flight = FLIGHTS.get(fkey)
    if not flight:
        return None, ("flight_not_found", "No operational record for %s." % fkey)

    # S1.2: measured from the departure airport of the AFFECTED segment to the final
    # destination. On a return itinerary each direction is its own journey.
    distance = _distance(segment["origin"], booking["final_destination"])
    if distance is None:
        distance = _distance(segment["origin"], segment["destination"])
    band = band_for(distance)
    if band is None:
        return {
            "booking_ref": booking_ref,
            "status": "INSUFFICIENT_DATA",
            "note": "No great-circle distance is held for this journey, so the entitlement "
                    "cannot be banded. S10.3 applies: assess manually and record that you "
                    "have done so.",
            "passengers": [],
        }, None
    cause = flight.get("cause_code")
    extraordinary = cause in EXTRAORDINARY_CAUSES
    arrival_delay = disruption.get("arrival_delay_minutes")
    rerouted = disruption.get("rerouted_onto")
    cancelled = flight.get("status") == "CANCELLED"
    dep_delay = flight.get("departure_delay_minutes")

    # ---- Section 5: statutory compensation ----------------------------------
    comp = {"amount_gbp": 0.0, "status": None, "reasoning": []}
    comp["reasoning"].append(
        "S1.3: great-circle distance %s km, therefore Band %s." % (distance, band))
    comp["reasoning"].append(
        "S3: operational record shows cause_code=%s, which is %s."
        % (cause, "an extraordinary circumstance (S3.2)" if extraordinary
           else "within Aerlink's control (S3.3)"))

    if extraordinary:
        comp["status"] = "NOT_PAYABLE"
        comp["reasoning"].append(
            "S5.1(b): cause is extraordinary, therefore no compensation is payable. "
            "Duty of care under S4 is unaffected.")
    elif arrival_delay is None:
        comp["status"] = "INSUFFICIENT_DATA"
        comp["reasoning"].append(
            "S5.2: arrival delay at final destination is not yet known, because the "
            "passenger has not been re-routed. Compensation cannot be assessed until "
            "the passenger's re-routing (or refusal) is settled. S10.3 applies.")
    elif arrival_delay < 180:
        comp["status"] = "NOT_PAYABLE"
        comp["reasoning"].append(
            "S5.1(c)/S5.2: arrival delay at final destination is %d minutes, which is "
            "below the 3 hour threshold. No compensation is payable. Note that departure "
            "delay is not the test." % arrival_delay)
    else:
        base = BAND_COMPENSATION_GBP[band]
        comp["reasoning"].append(
            "S5.1(c): arrival delay at final destination is %d minutes, at or above the "
            "3 hour threshold." % arrival_delay)
        comp["reasoning"].append("S5.3: Band %s standard amount is GBP %.2f." % (band, base))
        threshold = BAND_REDUCTION_THRESHOLD_MIN[band]
        if rerouted and arrival_delay < threshold:
            base = base / 2.0
            comp["reasoning"].append(
                "S5.4: passenger was re-routed (%s) and arrival delay %d minutes is below "
                "the Band %s reduction threshold of %d minutes, so the amount is reduced "
                "by 50%% to GBP %.2f." % (rerouted, arrival_delay, band, threshold, base))
        elif rerouted:
            comp["reasoning"].append(
                "S5.4: passenger was re-routed but arrival delay %d minutes is not below "
                "the Band %s reduction threshold of %d minutes, so no reduction applies."
                % (arrival_delay, band, threshold))
        else:
            comp["reasoning"].append("S5.4: no re-routing recorded, so no reduction applies.")
        comp["amount_gbp"] = round(base, 2)
        comp["status"] = "PAYABLE"

    # ---- Section 4: duty of care --------------------------------------------
    care_threshold = BAND_CARE_THRESHOLD_MIN[band]
    care_triggered = cancelled or (dep_delay is not None and dep_delay >= care_threshold)
    care = {
        "triggered": care_triggered,
        "basis": ("S4.1: flight cancelled, care owed immediately."
                  if cancelled else
                  "S4.1: departure delay %s minutes against Band %s threshold of %d minutes."
                  % (dep_delay, band, care_threshold)),
        "note": "S4 care is owed regardless of cause, including extraordinary circumstances (S4.3).",
        "entitlements": ([
            {"type": "meals_refreshments", "cap_gbp": 30.0, "per": "passenger per 6 hours"},
            {"type": "communication", "cap": "two calls or equivalent"},
            {"type": "hotel", "cap_gbp": 180.0, "per": "room per night", "max_nights": 3,
             "note": "Subject to station allocation, S4.5."},
            {"type": "transport", "cap_gbp": 45.0, "per": "journey"},
        ] if care_triggered else []),
    }

    # ---- Section 9: downgrade, and per-passenger roll-up ---------------------
    results = []
    for p in booking["passengers"]:
        if passenger_id and p["passenger_id"] != passenger_id:
            continue

        entry = {
            "passenger_id": p["passenger_id"],
            "name": "%s %s" % (p["given_name"], p["surname"]),
            "passenger_type": p.get("passenger_type"),
            "compensation_gbp": comp["amount_gbp"] if comp["status"] == "PAYABLE" else 0.0,
            "compensation_status": comp["status"],
            "downgrade_reimbursement_gbp": 0.0,
            "downgrade_reasoning": [],
            "flags": [],
        }

        if p.get("passenger_type") == "INFANT":
            entry["compensation_gbp"] = 0.0
            entry["compensation_status"] = "NOT_PAYABLE"
            entry["flags"].append("S5.3: infant not occupying a seat, no compensation.")

        if p.get("passenger_type") == "YTP":
            entry["flags"].append(
                "S13: Young Traveller Programme passenger. S12.1 requires supervisor "
                "authorisation for any action on this booking, and S13.2 prohibits "
                "re-routing, refund or amendment by a representative.")

        booked, flown = p.get("cabin_booked"), p.get("cabin_flown")
        if booked and flown and CABIN_RANK.get(flown, 0) < CABIN_RANK.get(booked, 0):
            pct = BAND_DOWNGRADE_PCT[band]
            seg_fare = segment.get("segment_fare_gbp", 0.0)
            amount = round(pct * seg_fare, 2)
            entry["downgrade_reimbursement_gbp"] = amount
            entry["downgrade_reasoning"] = [
                "S9.1: carried in %s having booked %s, so a downgrade occurred." % (flown, booked),
                "S9.1: Band %s reimburses %d%% of the affected segment fare."
                % (band, int(pct * 100)),
                "S9.4: the basis is segment %s fare of GBP %.2f, NOT the booking total of "
                "GBP %.2f and NOT taxes, charges or ancillaries."
                % (segment["segment_id"], seg_fare, booking.get("total_paid_gbp", 0.0)),
                "S9.3: this is in addition to any compensation under S5, not instead of it.",
                "Reimbursement: %d%% of GBP %.2f = GBP %.2f." % (int(pct * 100), seg_fare, amount),
            ]

        entry["total_payable_gbp"] = round(
            entry["compensation_gbp"] + entry["downgrade_reimbursement_gbp"], 2)
        results.append(entry)

    return {
        "booking_ref": booking_ref,
        "status": "ASSESSED",
        "authoritative": True,
        "policy_version": "APCP-2026-04 v11.3",
        "note": "S10.2: where this service returns a figure, that figure is the amount owed. "
                "It takes precedence over a figure derived by reading the policy text.",
        "journey": {
            "origin": segment["origin"],
            "final_destination": booking["final_destination"],
            "great_circle_distance_km": distance,
            "band": band,
            "affected_flight": fkey,
            "cause_code": cause,
            "cause_is_extraordinary": extraordinary,
            "flight_status": flight.get("status"),
            "departure_delay_minutes": dep_delay,
            "arrival_delay_minutes_at_final_destination": arrival_delay,
            "rerouted_onto": rerouted,
        },
        "compensation": comp,
        "duty_of_care": care,
        "passengers": results,
        "total_payable_gbp": round(sum(r["total_payable_gbp"] for r in results), 2),
    }, None


# --------------------------------------------------------------------------
# Booking search
# --------------------------------------------------------------------------

def _norm_phone(s):
    return re.sub(r"[^\d]", "", s or "")


def search_bookings(q):
    q = (q or "").strip()
    if not q:
        return []
    ql = q.lower()
    qphone = _norm_phone(q)
    out = []

    for ref, b in BOOKINGS.items():
        reasons = []
        if ql == ref.lower():
            reasons.append("booking_reference_exact")
        if ql == (b.get("contact_email") or "").lower():
            reasons.append("contact_email_exact")
        if qphone and len(qphone) >= 9 and qphone in _norm_phone(b.get("contact_phone")):
            reasons.append("contact_phone_match")
        for p in b["passengers"]:
            full = ("%s %s" % (p["given_name"], p["surname"])).lower()
            if ql == full:
                reasons.append("passenger_name_exact:%s" % p["passenger_id"])
            elif ql in full or full in ql:
                reasons.append("passenger_name_partial:%s" % p["passenger_id"])
            elif p["surname"].lower() == ql:
                reasons.append("passenger_surname_exact:%s" % p["passenger_id"])
        if reasons:
            out.append({
                "booking_ref": ref,
                "customer_id": b["customer_id"],
                "contact_email": b["contact_email"],
                "passengers": ["%s %s" % (p["given_name"], p["surname"]) for p in b["passengers"]],
                "segments": ["%s %s %s-%s" % (s["flight_no"], s["date"], s["origin"], s["destination"])
                             for s in b["segments"]],
                "matched_on": sorted(set(reasons)),
            })
    out.sort(key=lambda r: r["booking_ref"])
    return out


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AerlinkOps/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the console clean; use /_audit instead

    # -- helpers ----------------------------------------------------------

    def _send(self, code, payload, extra_headers=None):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        return code

    def _error(self, code, error, message, **extra):
        payload = {"error": error, "message": message}
        payload.update(extra)
        return self._send(code, payload)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def _audit(self, method, path, query, status, body=None, is_write=False):
        with STATE.lock:
            STATE.audit.append({
                "n": len(STATE.audit) + 1,
                "at": now_iso(),
                "method": method,
                "path": path,
                "query": query,
                "status": status,
                "is_write": is_write,
                "request_body": body,
            })

    def _rate_limited(self):
        now = time.time()
        with STATE.lock:
            STATE.request_times = [t for t in STATE.request_times if now - t < RATE_LIMIT_WINDOW_S]
            if len(STATE.request_times) >= RATE_LIMIT_REQUESTS:
                return True
            STATE.request_times.append(now)
        return False

    def _should_fail_flaky(self, key):
        """First attempt at any distinct query fails, then every 7th thereafter."""
        with STATE.lock:
            n = STATE.attempt_counts.get(key, 0) + 1
            STATE.attempt_counts[key] = n
        return n == 1 or n % 7 == 0

    # -- dispatch ---------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        body = self._body() if method == "POST" else None

        if path == "/health":
            self._send(200, {"status": "ok", "service": "aerlink-ops", "time": now_iso()})
            return

        if self.headers.get("X-Ops-Key") != API_KEY:
            status = self._error(401, "unauthorized", "Missing or incorrect X-Ops-Key header.")
            self._audit(method, path, query, status)
            return

        if body is None and method == "POST":
            status = self._error(400, "invalid_json", "Request body is not valid JSON.")
            self._audit(method, path, query, status)
            return

        if path not in ("/_audit", "/_reset") and self._rate_limited():
            status = self._error(
                429, "rate_limited",
                "More than %d requests in %.0f seconds." % (RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_S),
                extra_headers=None)
            self._audit(method, path, query, status)
            return

        try:
            status, is_write = self.route(method, path, query, body)
        except Exception as exc:  # never leak a stack trace to the client
            status = self._error(500, "internal_error", "%s: %s" % (type(exc).__name__, exc))
            is_write = False
        if path not in ("/_audit", "/_reset"):  # as documented in API.md
            self._audit(method, path, query, status, body if method == "POST" else None, is_write)

    # -- routes -----------------------------------------------------------

    def route(self, method, path, q, body):
        parts = [unquote(p) for p in path.strip("/").split("/") if p]

        # 1. GET /bookings/search
        if method == "GET" and parts[:2] == ["bookings", "search"]:
            if not q.get("q"):
                return self._error(400, "missing_parameter", "Query parameter 'q' is required."), False
            results = search_bookings(q["q"])
            return self._send(200, {
                "query": q["q"],
                "match_count": len(results),
                "results": results,
            }), False

        # 2. GET /bookings/{ref}
        if method == "GET" and len(parts) == 2 and parts[0] == "bookings":
            b = BOOKINGS.get(parts[1].upper())
            if not b:
                return self._error(404, "not_found", "No booking with reference %s." % parts[1]), False
            return self._send(200, b), False

        # 3. GET /flights/availability  (before /flights/{no})
        if method == "GET" and parts[:2] == ["flights", "availability"]:
            partners = len(parts) == 3 and parts[2] == "partners"
            if len(parts) == 3 and not partners:
                return self._error(404, "not_found", "Unknown path."), False
            for req in ("from", "to", "date"):
                if not q.get(req):
                    return self._error(
                        400, "missing_parameter",
                        "Query parameters 'from', 'to' and 'date' are required."), False

            key = "avail:%s:%s:%s:%s" % (q["from"], q["to"], q["date"], partners)
            if not partners and self._should_fail_flaky(key):
                return self._error(
                    503, "service_unavailable",
                    "Availability service temporarily unavailable. Retry."), False

            time.sleep(1.6 + (_rng(q["from"], q["to"], q["date"]).random() * 0.9))

            rows = generate_availability(q["from"].upper(), q["to"].upper(), q["date"], partners)

            after = q.get("after")
            if after and re.match(r"^\d{2}:\d{2}$", after):
                cutoff = int(after[:2]) * 60 + int(after[3:])
                rows = [r for r in rows if r["_sort"] >= cutoff]

            ref = q.get("booking_ref")
            baseline = None
            if ref and ref.upper() in BOOKINGS:
                bk = BOOKINGS[ref.upper()]
                seg = next((s for s in bk["segments"]
                            if s["segment_id"] == (bk.get("disruption") or {}).get("affected_segment")), None)
                if seg:
                    fl = FLIGHTS.get("%s:%s" % (seg["flight_no"], seg["date"]))
                    baseline = _scheduled_arrival_minutes(fl) if fl else None

            page_size = max(1, min(int(q.get("page_size", 20)), 100))
            page = max(1, int(q.get("page", 1)))
            total = len(rows)
            window = rows[(page - 1) * page_size: page * page_size]

            out = []
            for r in window:
                row = {k: v for k, v in r.items() if not k.startswith("_")}
                if baseline is not None:
                    row["arrival_delay_vs_original_minutes"] = r["_arrival_abs_min"] - baseline
                out.append(row)

            return self._send(200, {
                "query": {"from": q["from"], "to": q["to"], "date": q["date"],
                          "after": after, "booking_ref": ref,
                          "carrier_scope": "partner" if partners else "own"},
                "page": page,
                "page_size": page_size,
                "total_results": total,
                "total_pages": (total + page_size - 1) // page_size if total else 0,
                "results": out,
            }), False

        # 4. GET /flights/{flight_no}?date=
        if method == "GET" and len(parts) == 2 and parts[0] == "flights":
            if not q.get("date"):
                return self._error(400, "missing_parameter", "Query parameter 'date' is required."), False
            f = FLIGHTS.get("%s:%s" % (parts[1].upper(), q["date"]))
            if not f:
                return self._error(
                    404, "not_found",
                    "No operational record for %s on %s." % (parts[1].upper(), q["date"])), False
            return self._send(200, f), False

        # 5. GET /policy/search
        if method == "GET" and parts == ["policy", "search"]:
            if not q.get("q"):
                return self._error(400, "missing_parameter", "Query parameter 'q' is required."), False
            limit = max(1, min(int(q.get("limit", 5)), 20))
            results = policy_search(q["q"], limit)
            return self._send(200, {
                "query": q["q"],
                "match_count": len(results),
                "search_type": "lexical",
                "results": results,
            }), False

        # 6. GET /policy/document
        if method == "GET" and parts == ["policy", "document"]:
            return self._send(200, {
                "document_ref": "APCP-2026-04",
                "version": "11.3",
                "characters": len(POLICY_TEXT),
                "content": POLICY_TEXT,
            }), False

        # 7. GET /customers/{id}/history
        if method == "GET" and len(parts) == 3 and parts[0] == "customers" and parts[2] == "history":
            c = CUSTOMERS.get(parts[1].upper())
            if not c:
                return self._error(404, "not_found", "No customer %s." % parts[1]), False
            return self._send(200, c), False

        # 8. GET /entitlements/calculate
        if method == "GET" and parts == ["entitlements", "calculate"]:
            if not q.get("booking_ref"):
                return self._error(400, "missing_parameter",
                                   "Query parameter 'booking_ref' is required."), False
            result, err = calculate_entitlement(q["booking_ref"].upper(), q.get("passenger_id"))
            if err:
                return self._error(404, err[0], err[1]), False
            return self._send(200, result), False

        # 9. GET /stations/{iata}/hotel-allocation?night=
        if method == "GET" and len(parts) == 3 and parts[0] == "stations" and parts[2] == "hotel-allocation":
            night = q.get("night")
            if not night:
                return self._error(400, "missing_parameter", "Query parameter 'night' is required."), False
            key = "%s:%s" % (parts[1].upper(), night)
            base = STATIONS["hotel_allocation"].get(key)
            if not base:
                return self._error(404, "not_found",
                                   "No allocation held for %s on %s." % (parts[1].upper(), night)), False
            with STATE.lock:
                remaining = STATE.hotel_remaining.get(key, 0)
            out = dict(base)
            out["rooms_remaining"] = remaining
            return self._send(200, out), False

        # 10. GET /disruption/feed
        if method == "GET" and parts == ["disruption", "feed"]:
            return self._send(200, FEED), False

        # 11. POST /rebooking
        if method == "POST" and parts == ["rebooking"]:
            required = ["booking_ref", "passenger_ids", "option_id", "flight_no", "date"]
            missing = [r for r in required if not body.get(r)]
            if missing:
                return self._error(400, "missing_field",
                                   "Required: %s." % ", ".join(missing)), True
            if body["booking_ref"].upper() not in BOOKINGS:
                return self._error(404, "not_found",
                                   "No booking %s." % body["booking_ref"]), True
            with STATE.lock:
                rid = STATE.next_id("RBK")
                STATE.rebookings[rid] = {
                    "rebooking_id": rid, "confirmed_at": now_iso(), "status": "CONFIRMED", **body}
                rec = STATE.rebookings[rid]
            return self._send(201, rec), True

        # 12. POST /rebooking/{id}/cancel
        if method == "POST" and len(parts) == 3 and parts[0] == "rebooking" and parts[2] == "cancel":
            with STATE.lock:
                rec = STATE.rebookings.get(parts[1])
                if not rec:
                    return self._error(404, "not_found", "No rebooking %s." % parts[1]), True
                rec["status"] = "CANCELLED"
                rec["cancelled_at"] = now_iso()
                rec["cancellation_fee_gbp"] = 65.00
                out = dict(rec)
            return self._send(200, out), True

        # 13. POST /payments/goodwill
        if method == "POST" and parts == ["payments", "goodwill"]:
            for r in ("booking_ref", "amount_gbp", "reason"):
                if body.get(r) in (None, ""):
                    return self._error(400, "missing_field",
                                       "Required: booking_ref, amount_gbp, reason."), True
            try:
                amount = float(body["amount_gbp"])
            except (TypeError, ValueError):
                return self._error(400, "invalid_field", "amount_gbp must be a number."), True
            if body["booking_ref"].upper() not in BOOKINGS:
                return self._error(404, "not_found", "No booking %s." % body["booking_ref"]), True
            with STATE.lock:
                pid = STATE.next_id("GWP")
                STATE.payments[pid] = {
                    "payment_id": pid, "type": "GOODWILL", "paid_at": now_iso(),
                    "status": "PAID", "amount_gbp": round(amount, 2),
                    "booking_ref": body["booking_ref"].upper(), "reason": body["reason"]}
                rec = STATE.payments[pid]
            return self._send(201, rec), True

        # 14. POST /payments/compensation
        if method == "POST" and parts == ["payments", "compensation"]:
            for r in ("booking_ref", "amount_gbp"):
                if body.get(r) in (None, ""):
                    return self._error(400, "missing_field",
                                       "Required: booking_ref, amount_gbp."), True
            try:
                amount = float(body["amount_gbp"])
            except (TypeError, ValueError):
                return self._error(400, "invalid_field", "amount_gbp must be a number."), True
            if body["booking_ref"].upper() not in BOOKINGS:
                return self._error(404, "not_found", "No booking %s." % body["booking_ref"]), True
            with STATE.lock:
                pid = STATE.next_id("CMP")
                STATE.payments[pid] = {
                    "payment_id": pid, "type": "COMPENSATION", "paid_at": now_iso(),
                    "status": "PAID", "amount_gbp": round(amount, 2),
                    "booking_ref": body["booking_ref"].upper(),
                    "passenger_ids": body.get("passenger_ids"),
                    "reason": body.get("reason")}
                rec = STATE.payments[pid]
            return self._send(201, rec), True

        # 15. POST /refunds
        if method == "POST" and parts == ["refunds"]:
            for r in ("booking_ref", "passenger_ids", "amount_gbp"):
                if body.get(r) in (None, ""):
                    return self._error(400, "missing_field",
                                       "Required: booking_ref, passenger_ids, amount_gbp."), True
            if body["booking_ref"].upper() not in BOOKINGS:
                return self._error(404, "not_found", "No booking %s." % body["booking_ref"]), True
            with STATE.lock:
                rid = STATE.next_id("RFD")
                STATE.refunds[rid] = {
                    "refund_id": rid, "issued_at": now_iso(), "status": "ISSUED",
                    "amount_gbp": round(float(body["amount_gbp"]), 2),
                    "booking_ref": body["booking_ref"].upper(),
                    "passenger_ids": body["passenger_ids"]}
                rec = STATE.refunds[rid]
            return self._send(201, rec), True

        # 16. POST /vouchers/hotel
        if method == "POST" and parts == ["vouchers", "hotel"]:
            for r in ("booking_ref", "station", "night", "passenger_ids"):
                if body.get(r) in (None, ""):
                    return self._error(400, "missing_field",
                                       "Required: booking_ref, station, night, passenger_ids."), True
            key = "%s:%s" % (body["station"].upper(), body["night"])
            with STATE.lock:
                if key not in STATE.hotel_remaining:
                    return self._error(404, "not_found",
                                       "No allocation held for %s." % key), True
                if STATE.hotel_remaining[key] <= 0:
                    return self._error(
                        409, "allocation_exhausted",
                        "No rooms remain in the Aerlink allocation at %s for %s."
                        % (body["station"].upper(), body["night"])), True
                STATE.hotel_remaining[key] -= 1
                vid = STATE.next_id("HTL")
                STATE.vouchers[vid] = {
                    "voucher_id": vid, "issued_at": now_iso(), "status": "ISSUED",
                    "station": body["station"].upper(), "night": body["night"],
                    "booking_ref": body["booking_ref"].upper(),
                    "passenger_ids": body["passenger_ids"],
                    "rate_gbp": STATIONS["hotel_allocation"][key]["rate_gbp"],
                    "rooms_remaining_after": STATE.hotel_remaining[key]}
                rec = STATE.vouchers[vid]
            return self._send(201, rec), True

        # 17. POST /escalations
        if method == "POST" and parts == ["escalations"]:
            for r in ("summary", "requested_decision"):
                if body.get(r) in (None, ""):
                    return self._error(
                        400, "missing_field",
                        "Required: summary, requested_decision. Optional: booking_ref, "
                        "queue, recommendation, blocking_clause."), True
            with STATE.lock:
                eid = STATE.next_id("ESC")
                STATE.escalations[eid] = {
                    "escalation_id": eid, "raised_at": now_iso(), "status": "OPEN",
                    "queue": body.get("queue", "GENERAL"), **body}
                rec = STATE.escalations[eid]
            return self._send(201, rec), True

        # 18. GET /_audit
        if method == "GET" and parts == ["_audit"]:
            with STATE.lock:
                return self._send(200, {
                    "total_requests": len(STATE.audit),
                    "writes": {
                        "rebookings": list(STATE.rebookings.values()),
                        "refunds": list(STATE.refunds.values()),
                        "payments": list(STATE.payments.values()),
                        "hotel_vouchers": list(STATE.vouchers.values()),
                        "escalations": list(STATE.escalations.values()),
                    },
                    "totals": {
                        "money_paid_gbp": round(
                            sum(p["amount_gbp"] for p in STATE.payments.values())
                            + sum(r["amount_gbp"] for r in STATE.refunds.values()), 2),
                        "rebookings_confirmed": len(
                            [r for r in STATE.rebookings.values() if r["status"] == "CONFIRMED"]),
                        "hotel_vouchers_issued": len(STATE.vouchers),
                        "escalations_raised": len(STATE.escalations),
                    },
                    "requests": STATE.audit,
                }), False

        # 19. POST /_reset
        if method == "POST" and parts == ["_reset"]:
            with STATE.lock:
                STATE.reset()
            return self._send(200, {"status": "reset", "at": now_iso()}), False

        return self._error(404, "not_found", "No route for %s %s." % (method, path)), False


def main():
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        sys.stderr.write(
            "\nCould not start on %s:%d -- %s\n\n"
            "Something else is already using that port. If you carry on, requests will\n"
            "reach that other service instead of this one and the responses will make\n"
            "no sense. Free the port, or pick another:\n\n"
            "    OPS_PORT=9642 python3 ops_server.py\n\n"
            "and set OPS_BASE_URL in your .env to match.\n\n" % (HOST, PORT, exc))
        raise SystemExit(1)

    server.daemon_threads = True
    print("Aerlink Operations API listening on http://%s:%d" % (HOST, PORT))
    print("Send header  X-Ops-Key: %s" % API_KEY)
    print("Endpoint reference: API.md    Health check: GET /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
