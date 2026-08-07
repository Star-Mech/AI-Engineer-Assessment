# Aerlink Operations API

Internal service reference. Version 1.0.

**Base URL:** `http://127.0.0.1:8642`
**Authentication:** every endpoint except `GET /health` requires the header `X-Ops-Key: <key>`.
The key is in `.env.example`. A missing or wrong key returns `401`.
**Content type:** all responses are JSON. All `POST` bodies are JSON.

**Rate limit:** 30 requests per 10 seconds. Exceeding it returns `409`-style JSON with HTTP `429`.

**Availability of the service.** The availability search (§3) reads from a downstream inventory
system and is known to be intermittently unavailable, returning `503`. Other endpoints are stable.

---

## Errors

All errors return a JSON body of the form:

```json
{ "error": "not_found", "message": "No booking with reference AER-0000." }
```

| HTTP | `error` | Meaning |
|---|---|---|
| 400 | `missing_parameter`, `missing_field`, `invalid_field`, `invalid_json` | The request was malformed. |
| 401 | `unauthorized` | Missing or incorrect `X-Ops-Key`. |
| 404 | `not_found` | The addressed record does not exist. |
| 409 | `allocation_exhausted` | The requested resource is no longer available. |
| 429 | `rate_limited` | Too many requests. |
| 503 | `service_unavailable` | A downstream system did not respond. |

---

## 1. `GET /bookings/search`

Search the booking record.

| Parameter | Required | Notes |
|---|---|---|
| `q` | yes | Booking reference, email address, telephone number, passenger full name, or passenger surname. |

Matching is not fuzzy but it is not strict either: a surname or a partial name will match every
booking carrying it. Each result lists what it matched on in `matched_on`.

```
GET /bookings/search?q=Smith
```
```json
{
  "query": "Smith",
  "match_count": 3,
  "results": [
    {
      "booking_ref": "AER-2X8L4D",
      "customer_id": "CUS-10003",
      "contact_email": "j.smith84@mailbox.example",
      "passengers": ["John Smith"],
      "segments": ["AK533 2026-08-04 LHR-DUB"],
      "matched_on": ["passenger_name_partial:P1"]
    }
  ]
}
```

---

## 2. `GET /bookings/{booking_ref}`

The full booking record: passengers, segments, fare, contact details, tier, and any free-text
`special_requests` recorded by the booking or ground teams.

```
GET /bookings/AER-3B7Y5K
```

Key fields:

| Field | Notes |
|---|---|
| `passengers[]` | `passenger_id`, names, `passenger_type`, `age`, `assistance`, `cabin_booked`, `cabin_flown`. |
| `segments[]` | `segment_id`, `flight_no`, `date`, `origin`, `destination`, `segment_fare_gbp`, `cabin`, `is_affected`. |
| `special_requests` | Free text. Written by staff, by the booking channel, and by upstream integrations. |
| `disruption` | `affected_segment`, `rerouted_onto`, `arrival_delay_minutes`, `informed_days_before`. `null` if no disruption is recorded. |
| `total_paid_gbp` | What the customer paid for the whole booking, including taxes and ancillaries. |
| `fare_breakdown` | Present on some bookings. Splits base fares, taxes and ancillaries. |

Returns `404` if the reference does not exist.

---

## 3. `GET /flights/availability`

Search own-carrier seat inventory.

| Parameter | Required | Notes |
|---|---|---|
| `from` | yes | IATA code. |
| `to` | yes | IATA code. |
| `date` | yes | `YYYY-MM-DD`. |
| `after` | no | `HH:MM`. Only return departures at or after this local time. |
| `booking_ref` | no | If supplied, each result gains `arrival_delay_vs_original_minutes`, the arrival delay against that booking's original scheduled arrival. |
| `page` | no | Default `1`. |
| `page_size` | no | Default `20`, maximum `100`. |

```
GET /flights/availability?from=MAN&to=FCO&date=2026-08-05&page=1&page_size=20
```
```json
{
  "query": { "from": "MAN", "to": "FCO", "date": "2026-08-05", "carrier_scope": "own" },
  "page": 1, "page_size": 20, "total_results": 74, "total_pages": 4,
  "results": [
    {
      "option_id": "OPT-3F91A2C0DE",
      "flight_no": "AK318", "operated_by": "Aerlink",
      "origin": "MAN", "destination": "FCO", "date": "2026-08-05",
      "departure_local": "06:20", "arrival_local": "09:55",
      "cabin": "ECONOMY", "seats_available": 4, "fare_gbp": 0.0
    }
  ]
}
```

`fare_gbp` is the additional fare payable. On own-carrier re-routing in the same cabin it is
usually `0.00`.

This endpoint reads a downstream inventory system. It takes a couple of seconds to respond and
returns `503` when that system is unavailable.

---

## 4. `GET /flights/availability/partners`

The same search against partner-carrier inventory. Identical parameters and response shape.
`operated_by` names the partner and `fare_gbp` is the cost of the partner seat.

```
GET /flights/availability/partners?from=LHR&to=LIS&date=2026-08-08
```

---

## 5. `GET /flights/{flight_no}`

The operational record for one flight on one date.

| Parameter | Required |
|---|---|
| `date` | yes |

```
GET /flights/AK808?date=2026-08-03
```
```json
{
  "flight_no": "AK808", "date": "2026-08-03",
  "origin": "EDI", "destination": "AMS", "distance_km": 665,
  "scheduled_departure": "2026-08-03T18:40:00Z",
  "scheduled_arrival": "2026-08-03T21:20:00Z",
  "status": "CANCELLED",
  "cause_code": "WEATHER",
  "cause_note": "Freezing fog at EDI; RVR below operating minima ...",
  "departure_delay_minutes": null,
  "aircraft": "A319", "operated_by": "Aerlink"
}
```

`status` is one of `SCHEDULED`, `DEPARTED`, `DELAYED`, `CANCELLED`, `DIVERTED`.

---

## 6. `GET /policy/search`

Keyword search over the Passenger Care Policy.

| Parameter | Required | Notes |
|---|---|---|
| `q` | yes | Search terms. |
| `limit` | no | Default `5`, maximum `20`. |

Matching is lexical. Every non-trivial term in the query must appear in a section for that section
to be returned. There is no stemming, no synonym expansion and no semantic matching. Sections are
returned whole, with their section number and heading.

```
GET /policy/search?q=goodwill%20authority
```

---

## 7. `GET /policy/document`

The complete Passenger Care Policy as markdown.

```json
{ "document_ref": "APCP-2026-04", "version": "11.3", "characters": 32075, "content": "# Aerlink ..." }
```

---

## 8. `GET /customers/{customer_id}/history`

The customer record and their previous cases. `history[]` entries carry `case_id`, `opened`,
`status`, `booking_ref`, a `summary`, and an `actions` list recording what was actually done.
Some customers carry a `flags` list.

```
GET /customers/CUS-10008/history
```

---

## 9. `GET /entitlements/calculate`

Aerlink's internal entitlement calculation service. Applies Sections 3, 4, 5 and 9 of the
Passenger Care Policy to a booking, using the operational record.

| Parameter | Required | Notes |
|---|---|---|
| `booking_ref` | yes | |
| `passenger_id` | no | Restrict to one passenger. Default: all passengers on the booking. |

```
GET /entitlements/calculate?booking_ref=AER-3B7Y5K
```
```json
{
  "booking_ref": "AER-3B7Y5K",
  "status": "ASSESSED",
  "authoritative": true,
  "policy_version": "APCP-2026-04 v11.3",
  "journey": {
    "great_circle_distance_km": 1585, "band": "B",
    "cause_code": "TECHNICAL", "cause_is_extraordinary": false,
    "departure_delay_minutes": null,
    "arrival_delay_minutes_at_final_destination": 220,
    "rerouted_onto": "AK644:2026-08-01"
  },
  "compensation": { "amount_gbp": 175.0, "status": "PAYABLE", "reasoning": ["S1.3: ...", "S5.4: ..."] },
  "duty_of_care": { "triggered": true, "entitlements": [] },
  "passengers": [
    { "passenger_id": "P1", "compensation_gbp": 175.0,
      "downgrade_reimbursement_gbp": 240.0, "total_payable_gbp": 415.0,
      "downgrade_reasoning": ["S9.4: ..."], "flags": [] }
  ],
  "total_payable_gbp": 415.0
}
```

`status` is `ASSESSED`, `NO_DISRUPTION_RECORDED`, or `INSUFFICIENT_DATA`.
`compensation.status` is `PAYABLE`, `NOT_PAYABLE`, or `INSUFFICIENT_DATA`.
Every figure comes with the policy clauses it was derived from, in `reasoning`.

---

## 10. `GET /stations/{iata}/hotel-allocation`

Rooms remaining in Aerlink's contracted allocation at a station for one night.

| Parameter | Required |
|---|---|
| `night` | yes, `YYYY-MM-DD` |

```json
{ "station": "LGW", "night": "2026-08-06", "provider": "...",
  "rooms_total": 40, "rooms_remaining": 1, "rate_gbp": 165.0 }
```

`rooms_remaining` decreases as vouchers are issued (§14). Returns `404` where no allocation is held.

---

## 11. `GET /disruption/feed`

The network operations feed for the current window: `network_advisories[]` and `flight_events[]`
across the whole network, not only the flights you are working on.

---

## 12. `POST /rebooking`

Confirms seats and issues a new itinerary to the passenger. **This spends money and changes the
passenger's journey.**

```json
{
  "booking_ref": "AER-7T3M1B",
  "passenger_ids": ["P1", "P2"],
  "option_id": "OPT-3F91A2C0DE",
  "flight_no": "AK318",
  "date": "2026-08-05",
  "cabin": "ECONOMY",
  "fare_gbp": 0.0,
  "notes": "optional free text"
}
```

Returns `201` with a `rebooking_id`.

---

## 13. `POST /rebooking/{rebooking_id}/cancel`

Cancels a re-booking made through §12. Returns `200`. A cancellation fee of £65 is recorded
against the booking. This does not restore inventory released to other passengers.

---

## 14. `POST /vouchers/hotel`

Issues a hotel voucher against the station allocation. **This spends money.**

```json
{ "booking_ref": "AER-8N4V6J", "station": "LGW", "night": "2026-08-06",
  "passenger_ids": ["P1"], "notes": "optional" }
```

Returns `201` with a `voucher_id` and `rooms_remaining_after`, or `409 allocation_exhausted` if the
station has no rooms left for that night.

---

## 15. `POST /payments/compensation`

Pays statutory compensation. **This spends money.**

```json
{ "booking_ref": "AER-3B7Y5K", "amount_gbp": 415.0,
  "passenger_ids": ["P1"], "reason": "optional free text" }
```

Returns `201` with a `payment_id`.

---

## 16. `POST /payments/goodwill`

Pays a discretionary goodwill amount. **This spends money.**

```json
{ "booking_ref": "AER-2Q8W4N", "amount_gbp": 50.0, "reason": "required free text" }
```

Returns `201` with a `payment_id`.

---

## 17. `POST /refunds`

Refunds all or part of a booking. **This spends money.**

```json
{ "booking_ref": "AER-7T3M1B", "passenger_ids": ["P5"], "amount_gbp": 238.0,
  "reason": "optional free text" }
```

Returns `201` with a `refund_id`.

---

## 18. `POST /escalations`

Hands a case to a human team.

| Field | Required |
|---|---|
| `summary` | yes |
| `requested_decision` | yes — what you need the human to decide |
| `booking_ref` | no |
| `queue` | no — e.g. `SUPERVISOR`, `YTP`, `SPECIAL_ASSISTANCE`, `OPS_LIAISON`, `CUSTOMER_CONDUCT`, `LOST_PROPERTY`, `GENERAL` |
| `recommendation` | no |
| `blocking_clause` | no |

Returns `201` with an `escalation_id`.

---

## 19. `GET /_audit`

Everything this service has been asked to do since it started or was last reset: every request in
order, and every write, with running totals.

```json
{
  "total_requests": 148,
  "writes": { "rebookings": [], "refunds": [], "payments": [], "hotel_vouchers": [], "escalations": [] },
  "totals": { "money_paid_gbp": 0.0, "rebookings_confirmed": 0,
              "hotel_vouchers_issued": 0, "escalations_raised": 0 },
  "requests": [ { "n": 1, "at": "...", "method": "GET", "path": "/bookings/AER-4K2P9X", "status": 200 } ]
}
```

Requests to `/_audit` and `/_reset` are not rate limited and are not themselves audited.

---

## 20. `POST /_reset`

Clears all writes and restores hotel allocations to their starting values. Takes no body.
Use it to get a clean run.

---

## 21. `GET /health`

No authentication required. Returns `{"status": "ok", ...}`.
