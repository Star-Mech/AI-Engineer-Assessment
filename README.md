# Aerlink Disruption Desk

A system that works inbound passenger disruption cases: it reads the message, establishes who
is writing and what actually happened from Aerlink's own records, works out what is owed,
takes the actions it judges appropriate, and produces a record of what it did and why.

---

## Run it

Three commands from a clean clone.

```bash
pip install -r requirements.txt
```
```bash
cp .env.example .env   # then put your OpenAI key in OPENAI_API_KEY
```
```bash
python env/ops_server.py
```

Then, in a second terminal — **this is the one command**:

```bash
python run.py --all --execute --yes
```

Records land in `runs/<timestamp>/`: one JSON per case, plus `_audit.json` (a snapshot of the
operations API's own permanent log), `_summary.json`, and `trace.jsonl`.

The submitted run is the single `runs/<timestamp>/` directory produced by that one command:
twelve case records, the `_audit.json` snapshot taken at the end, `_summary.json` with the
measured spend, and `trace.jsonl`.

### Watching a run

Runs print a live trace to stderr and write `trace.jsonl` — every ops call with its status and
latency, every agent turn with its token counts, every tool call, every gate verdict. A case
involving re-routing takes minutes (the availability endpoint sleeps ~2s per call and fails the
first attempt at every distinct query by design), so a silent run is indistinguishable from a
hung one. `--quiet` suppresses stderr; `trace.jsonl` is written either way.

### Working a case we have not shown you

Make a directory with the same two files every case in `cases/` has:

```
mycase/
├── inbound.txt     the raw message, headers and all
└── meta.json       {"case_id","channel","received_at","from","subject"}
```

```bash
python run.py --case mycase --execute --yes
```

Only `case_id` and `received_at` are load-bearing — `received_at` is UTC and is treated as the
case's "now". Everything operational (booking reference, flight, passengers, language, intent)
is extracted from the message itself.

### The flags that matter

| Flag | Effect |
|---|---|
| *(none)* | **Dry run.** Writes are simulated; `GET /_audit` is untouched. |
| `--execute` | Issue real writes. |
| `--yes` | **Required for any run that calls OpenAI.** Guards a capped key. |
| `--offline` | Replay recorded fixtures. Makes no OpenAI calls at all. |
| `--cases A B C` | A subset. |
| `--reset` | `POST /_reset` first. |
| `--quiet` | Suppress the live trace on stderr (`trace.jsonl` is written either way). |
