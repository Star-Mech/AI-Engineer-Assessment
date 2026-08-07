# Aerlink Disruption Desk — practical exercise

Thank you for making the time. This is the practical part of our process for the AI Engineer role.

**Timebox: three hours.** Self-timed, whenever suits you inside the window we agreed. Please don't
go far over — we would much rather see three focused hours and an honest account of what you left
out than a weekend of polishing.

We will spend the interview afterwards talking through what you built and why, so the reasoning
matters at least as much as the result.

---

## The situation

Aerlink is a mid-sized airline. When flights are cancelled, delayed or diverted, affected passengers
write in. A small team called the **disruption desk** picks up each of those messages and works the
case.

Working a case means, roughly:

- establishing who is writing and which booking they mean;
- establishing what actually happened to the flight, from Aerlink's own records rather than from
  the passenger's account;
- working out what the passenger is owed under the Passenger Care Policy, and what they are asking
  for, which are frequently not the same thing;
- finding out what options actually exist right now;
- and then either resolving it, or handing it to a human with enough for them to decide quickly.

The desk is drowning. Volume roughly tripled after a bad summer and the team is making mistakes —
mostly paying the wrong amount, occasionally acting on the wrong booking.

## What you are building

**A system that works these cases.**

It reads an inbound passenger message, does whatever it needs to do to understand and handle it,
takes the actions it judges appropriate, and produces a record of what it did and why.

Build it however you like. Any language, any libraries, any structure, any design. We have no
house style here and we are not looking for a particular shape of solution — two good candidates
have every reason to hand in quite different systems.

**One constraint: it must use OpenAI models.** We will send you an API key separately. It has a
hard spending cap of roughly $15–20, so it will not survive being pointed at a runaway loop.
Use it however you want within that.

## What you are given

```
candidate/
├── README.md                 this file
├── DECISIONS_TEMPLATE.md     copy to DECISIONS.md and fill in — a required deliverable
├── .env.example              copy to .env
├── env/
│   ├── ops_server.py         the Aerlink operations API (run this locally)
│   ├── API.md                its endpoint reference — read this properly
│   ├── data/                 the records it serves, including the Passenger Care Policy
│   └── Dockerfile, docker-compose.yml
└── cases/case-01 … case-12/  twelve real cases: inbound.txt and meta.json
```

The **operations API** is the desk's window onto the airline: bookings, flights, the policy,
customer history, seat availability, hotel allocation, and the endpoints that actually do things —
re-book a passenger, issue a voucher, make a payment, hand a case to a human.

The **twelve cases** are real ones, lightly anonymised, with all the mess that implies. They are in
no particular order and they are not of uniform difficulty.

### Running it

```bash
cp .env.example .env
python3 env/ops_server.py            # http://127.0.0.1:8642 — Python 3, no installation needed
curl http://127.0.0.1:8642/health
```

Or `cd env && docker compose up` if you'd rather not use the host Python. Everything is local;
nothing calls out to the internet except your own calls to OpenAI.

`POST /_reset` returns the API to its starting state, so you can re-run cleanly as often as you
like.

## Requirements

1. **Work a case end to end**, from the raw inbound message to either a resolution or a handover
   to a human.

2. **Work for a case we have not shown you.** We will run your system against inbound messages
   that are not in `cases/`. There must be one documented way to hand it a new case.

3. **Produce a record for each case.** Its shape is entirely your choice, but it must capture at
   least:
   - what you decided and what the passenger gets;
   - why — the reasoning, and what it rests on;
   - what was consulted to reach it;
   - what was actually done: every action taken against a booking or a payment;
   - anything you remain uncertain about;
   - what a human still needs to do, if anything.

4. **Actions are real.** The re-booking, payment, refund and voucher endpoints genuinely act:
   they move money and change passengers' journeys, and every one is logged permanently. Treat
   them the way you would treat the real thing.

5. **Stay inside the budget.** A full run across all twelve cases must cost **under $2.00**.
   Report your actual spend and token usage in `DECISIONS.md`. (This is a real constraint on the
   desk — it runs thousands of these a week.)

   A practical note: your key will not survive many full runs at that ceiling. Develop against
   one or two cases and save the full sweep for when you actually need it. If you deliberately
   go over the budget for a reason you can defend, that is a legitimate answer — tell us the
   reason and the number rather than quietly trimming something that mattered.

6. **We must be able to run it.** From a clean clone, with our own key, following your README, in
   one documented command.

7. **Give us a way to check it behaves correctly.** However you like. We are interested in what
   you consider worth checking and how you went about it.

### On finishing

Three hours is not enough to do all of this well. **Deciding what to do properly and what to leave
out is part of what we are assessing.** We would far rather see three requirements done thoroughly,
with a clear account of why you dropped the rest, than seven done thinly. Tell us what you chose
and why.

## What to send us

1. **The code**, with a README covering setup and the one command to run it.
2. **`DECISIONS.md`** — copy `DECISIONS_TEMPLATE.md` and work through it. This is not an
   afterthought; it is the document the interview is built around. Budget real time for it.
3. **The output of a full run** over the twelve cases, in whatever form your system produces.
4. **Whatever you built to check it works.**

Send a zip or a link to a repository.

## Using AI assistants

**Please use them.** Claude Code, Cursor, ChatGPT, whatever you normally reach for — we use these
tools every day here and we are not interested in watching you type from memory.

The only thing we ask is that you tell us where you used them, and — more interestingly — where
you overrode them. There is a section for this in `DECISIONS.md`. Saying "I used Claude Code for
most of the scaffolding" is a completely fine answer. Saying nothing, when you clearly did, is not.

## A note on what we care about

We are not counting features. We are trying to understand how you think about a problem like this
one: how you broke it up, what you decided to trust, what you decided to be careful about, and
what you would do next with more time.

If you finish something and think "that isn't right, but it will do for now" — write that down.
Noticing it is the valuable part, and we will ask you about it.

## Questions

Anything unclear or apparently contradictory in the brief, ask us — that is not a penalty and we
will answer quickly. Anything unclear in the **cases** is part of the exercise: make a call,
write down the assumption, and move on.

Good luck.
