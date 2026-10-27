"""Split a billing-guidelines document into citable clauses.

Guidelines arrive as Markdown or plain text exported from Word/PDF. Clients number them differently
("3.2", "§ 4.1", "Section 5(b)", "(c)"); a clause is the smallest numbered unit, with the headings above it
kept as its path. Unnumbered paragraphs get an id from their section and position ("4.p2"), so every
sentence of the document can be cited.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_NUMBERED = re.compile(
    r"^(?:§\s*|section\s+|clause\s+|article\s+)?(?P<id>\d{1,2}(?:\.\d{1,2}){0,3})(?P<sub>\([a-z0-9]{1,3}\))?[.):]?\s+(?P<rest>\S.*)$",
    re.IGNORECASE,
)
_LETTERED = re.compile(r"^\(?(?P<id>[a-z]|[ivx]{1,4})\)\s+(?P<rest>\S.*)$")
_HEADING_NUMBER = re.compile(
    r"^(?:§\s*|section\s+|part\s+|article\s+)?(?P<id>\d{1,2}(?:\.\d{1,2}){0,2}|[A-Z])[.):]?\s+(?P<title>.+)$", re.IGNORECASE
)


@dataclass(frozen=True)
class Clause:
    id: str
    path: tuple[str, ...]  # headings above the clause, outermost first
    text: str
    start: int  # character offsets in the document
    end: int

    @property
    def breadcrumb(self) -> str:
        return " > ".join(self.path)


@dataclass
class GuidelineDocument:
    tenant: str
    version: int
    effective_from: date
    title: str
    text: str
    clauses: list[Clause] = field(default_factory=list)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    @property
    def ref(self) -> str:
        return f"{self.tenant}/guidelines@v{self.version}"

    def clause(self, clause_id: str) -> Clause | None:
        return next((c for c in self.clauses if c.id == clause_id), None)


def parse(text: str, tenant: str, version: int, effective_from: date) -> GuidelineDocument:
    title = ""
    path: list[tuple[int, str]] = []  # (level, heading)
    section_id = ""
    clauses: list[Clause] = []
    unnumbered: dict[str, int] = {}
    current: dict | None = None
    offset = 0

    def flush() -> None:
        nonlocal current
        if current and current["text"].strip():
            body = re.sub(r"\s+", " ", current["text"]).strip()
            clauses.append(Clause(current["id"], current["path"], body, current["start"], current["end"]))
        current = None

    for raw in text.splitlines(keepends=True):
        line = raw.strip()
        start, end = offset, offset + len(raw)
        offset = end
        if not line:
            # a blank line ends an unnumbered paragraph; numbered clauses may span blank lines only via continuation
            if current and current.get("unnumbered"):
                flush()
            continue
        h = _HEADING.match(line)
        if h:
            flush()
            level, heading = len(h.group(1)), h.group(2).strip()
            if level == 1 and not title:
                title = heading
                continue
            path = [p for p in path if p[0] < level] + [(level, heading)]
            hn = _HEADING_NUMBER.match(heading)
            section_id = hn.group("id") if hn else heading[:24]
            continue
        m = _NUMBERED.match(line)
        lm = _LETTERED.match(line) if not m else None
        if m:
            flush()
            cid = m.group("id") + (m.group("sub") or "")
            current = {"id": cid, "path": tuple(p[1] for p in path), "text": line, "start": start, "end": end}
            continue
        if lm and current and not current.get("unnumbered"):
            parent = re.sub(r"\(.*\)$", "", current["id"])
            flush()
            cid = f"{parent}({lm.group('id')})"
            current = {"id": cid, "path": tuple(p[1] for p in path), "text": line, "start": start, "end": end}
            continue
        if line.startswith(("- ", "* ", "• ")) and current:
            current["text"] += " " + line
            current["end"] = end
            continue
        if current:
            current["text"] += " " + line
            current["end"] = end
            continue
        key = section_id or "0"
        unnumbered[key] = unnumbered.get(key, 0) + 1
        current = {
            "id": f"{key}.p{unnumbered[key]}",
            "path": tuple(p[1] for p in path),
            "text": line,
            "start": start,
            "end": end,
            "unnumbered": True,
        }
    flush()
    # ids must be unique to be citable; a repeated id (a numbering slip in the source) gets a suffix
    seen: dict[str, int] = {}
    unique: list[Clause] = []
    for c in clauses:
        n = seen.get(c.id, 0)
        seen[c.id] = n + 1
        unique.append(c if n == 0 else Clause(f"{c.id}~{n + 1}", c.path, c.text, c.start, c.end))
    return GuidelineDocument(tenant, version, effective_from, title or "Billing guidelines", text, unique)
