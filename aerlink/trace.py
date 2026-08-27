"""Execution tracing.

A run can spend several minutes inside one case -- the availability search alone sleeps
~2s per call and deliberately fails the first attempt at every distinct query -- so a silent
run gives no way to tell progress from a hang. Every ops call, every agent turn, every tool
call and every gate verdict is emitted here as it happens.

Two sinks: a readable line to stderr (so stdout stays clean for the summary), and a JSONL
file per run, which is what you actually want when reconstructing why a case did something
odd three days later.

Module-level singleton rather than a threaded-through dependency. Tracing that is awkward to
call does not get called.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, TextIO

# Two-letter tags keep the left margin narrow enough that the message stays readable.
TAGS = {
    "case": "==", "phase": "->", "ops": "  ", "llm": "AI", "tool": " *",
    "gate": "!!", "action": " +", "warn": "!W", "info": "  ",
}


class Tracer:
    def __init__(self) -> None:
        self.enabled = False
        self.t0 = time.time()
        self._fh: TextIO | None = None
        self._case: str | None = None

    def start(self, enabled: bool, path: Path | None = None) -> None:
        self.enabled = enabled
        self.t0 = time.time()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("w", encoding="utf-8")

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def case(self, case_id: str) -> None:
        self._case = case_id
        self.t0 = time.time()
        self.emit("case", "%s starting" % case_id)

    def emit(self, kind: str, message: str, **fields: Any) -> None:
        elapsed = time.time() - self.t0
        if self._fh:
            self._fh.write(json.dumps({
                "t": round(elapsed, 3), "case": self._case, "kind": kind,
                "message": message, **fields}, default=str) + "\n")
            self._fh.flush()
        if not self.enabled:
            return
        extra = "  " + "  ".join("%s=%s" % (k, _short(v)) for k, v in fields.items()) \
            if fields else ""
        sys.stderr.write("  %6.1fs %s %s%s\n" % (elapsed, TAGS.get(kind, "  "), message, extra))
        sys.stderr.flush()

    # -- convenience shapes ------------------------------------------------

    def phase(self, name: str, detail: str = "") -> None:
        self.emit("phase", "%s%s" % (name, (" -- " + detail) if detail else ""))

    def ops(self, method: str, path: str, status: Any, ms: float, note: str = "") -> None:
        self.emit("ops", "%-4s %-46s %s  %dms%s"
                  % (method, path[:46], status, ms, ("  " + note) if note else ""))

    def turn(self, n: int, model: str, tokens_in: int, tokens_out: int, cached: int) -> None:
        self.emit("llm", "turn %d  %s  in=%d (cached %d) out=%d"
                  % (n, model, tokens_in, cached, tokens_out))

    def tool(self, name: str, args: dict) -> None:
        self.emit("tool", "%-26s %s" % (name, _short(args, 140)))

    def gate(self, kind: str, clause: str | None) -> None:
        if clause:
            self.emit("gate", "REFUSED  %-14s %s" % (kind, clause))
        else:
            self.emit("gate", "approved %s" % kind)

    def action(self, kind: str, outcome: str, detail: str = "") -> None:
        self.emit("action", "%-9s %-8s %s" % (kind, outcome, detail))

    def warn(self, message: str, **fields: Any) -> None:
        self.emit("warn", message, **fields)


def _short(value: Any, limit: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


TRACE = Tracer()
