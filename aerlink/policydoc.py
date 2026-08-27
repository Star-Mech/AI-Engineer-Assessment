"""The Passenger Care Policy, fetched at runtime and indexed by structure.

Why not `/policy/search`. That endpoint is lexical AND matching with no stemming, no
synonyms and no semantics, so every non-stopword term must appear literally in a section
and adding terms *narrows* the result set. Probed against the live service:

    wheelchair          -> 0 hits   (the policy says "mobility device")
    unaccompanied minor -> 0 hits   (the policy says "Young Traveller Programme")
    escalate            -> 0 hits   (the policy says "referral" / "referred")
    authorization       -> 0 hits   (British spelling only)

The failure mode is not bad ranking, it is zero results from the wrong word — and
"escalate" returning nothing is fatal for a system whose main job is knowing when to refer.

So the policy is indexed by **structure** instead. The document is fetched from
`/policy/document` at startup and parsed here, which means nothing about the policy is
hard-coded: if Aerlink revises it, this picks up the revision on the next run. At its
current size (69 sections, ~8k tokens) the whole thing is inlined into the agent's cached
system prefix, which sidesteps retrieval entirely. Above `INLINE_MAX_CHARS` it degrades to
a table of contents plus `section()` lookups on demand.

The chunking mirrors `ops_server.py:_chunk_policy` so that section numbers here agree with
the ones `/policy/search` returns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Above this, stop inlining the whole document and hand the agent a TOC plus a lookup tool.
INLINE_MAX_CHARS = 40_000

# Roughly how much of a section body to show as its TOC hint.
HINT_CHARS = 110


@dataclass(frozen=True)
class Section:
    number: str | None      # "5.4", or None for the preamble
    heading: str            # "5.4 Reduction for re-routing"
    text: str
    level: int              # 2 for "##", 3 for "###"

    @property
    def title(self) -> str:
        """The heading with its section number stripped off."""
        m = re.match(r"^[\d.]+\s+(.*)$", self.heading)
        return m.group(1) if m else self.heading


class PolicyDoc:
    def __init__(self, document_ref: str, version: str, content: str):
        self.document_ref = document_ref
        self.version = version
        self.content = content
        self.sections: list[Section] = _chunk(content)
        self._by_number = {s.number: s for s in self.sections if s.number}

    @classmethod
    def fetch(cls, ops) -> PolicyDoc:
        doc = ops.policy_document()
        return cls(doc["document_ref"], doc["version"], doc["content"])

    # -- presentation to the model ----------------------------------------

    @property
    def inline_ok(self) -> bool:
        return len(self.content) <= INLINE_MAX_CHARS

    def toc(self) -> str:
        """Section numbers, headings and a one-line hint at what each contains.

        Generated from the fetched document, never hand-written, so it cannot go stale
        against a revised policy.
        """
        lines = ["%s v%s — %d sections" % (self.document_ref, self.version, len(self.sections))]
        for s in self.sections:
            if not s.number:
                continue
            hint = " ".join(s.text.split())
            if len(hint) > HINT_CHARS:
                hint = hint[:HINT_CHARS].rsplit(" ", 1)[0] + "..."
            indent = "  " if s.level == 3 else ""
            lines.append("%s%-6s %-52s %s" % (indent, s.number, s.title, hint))
        return "\n".join(lines)

    def section(self, numbers: str | list[str]) -> str:
        """Full text of one or more sections, addressed by number."""
        if isinstance(numbers, str):
            numbers = [numbers]
        out = []
        for n in numbers:
            n = n.strip().lstrip("Ss§").rstrip(".")
            sec = self._by_number.get(n)
            if sec is None:
                near = [k for k in self._by_number if k.startswith(n + ".")]
                if near:
                    out.append("Section %s is a container. Its subsections are: %s"
                               % (n, ", ".join(sorted(near))))
                else:
                    out.append("No section %s in %s v%s." % (n, self.document_ref, self.version))
                continue
            out.append("## %s\n\n%s" % (sec.heading, sec.text))
        return "\n\n---\n\n".join(out)

    def free_text_of(self, number: str) -> str | None:
        sec = self._by_number.get(number)
        return sec.text if sec else None


def _chunk(text: str) -> list[Section]:
    """Split on ## and ### headings. Mirrors ops_server.py:_chunk_policy."""
    chunks: list[dict] = []
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

    out: list[Section] = []
    for c in chunks:
        body = "\n".join(c["lines"]).strip()
        if not body:
            continue
        m = re.match(r"^([\d.]+)\s+(.*)$", c["heading"])
        out.append(Section(
            number=m.group(1).rstrip(".") if m else None,
            heading=c["heading"],
            text=body,
            level=c["level"],
        ))
    return out
