"""Named entities in time entries, resolved against the firm's roster.

spaCy finds PERSON spans; a roster gazetteer resolves them (full name, "F. Last", surname alone, initials) to
timekeepers at the firm. That one fact decides internal-conference questions: a meeting with a colleague is
internal, a meeting with the client's lawyer is not. Names that do not resolve stay external.

Before any text goes to a language model, people are replaced by stable placeholders (PERSON_1, ...) with their
role attached, so the model gets the fact it needs (at the firm or not) without the personal data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from typing import Any

import spacy

NOT_INITIALS = frozenset({"TC", "RE", "OC", "SJ", "AI", "US", "UK", "EU", "OK", "PM", "AM", "ID", "IP", "HR", "IT", "QA", "CV"})
_TITLE = re.compile(r"^(?:dr|prof|mr|mrs|ms|judge|justice)\.?\s+", re.I)


@cache
def nlp() -> Any:
    return spacy.load("en_core_web_md", disable=["lemmatizer"])


@dataclass(frozen=True)
class Mention:
    text: str
    start: int
    end: int
    label: str  # PERSON, GPE, ORG, DATE...
    timekeeper_id: str | None = None  # resolved to someone at the firm


class Roster:
    """Name forms of everyone at one firm."""

    def __init__(self, people: list[tuple[str, str]]):
        self.people = people
        self.full: dict[str, str] = {}
        self.initial_last: dict[str, str] = {}
        self.last: dict[str, list[str]] = {}
        self.initials: dict[str, list[str]] = {}
        for tk_id, name in people:
            parts = name.split()
            first, last = parts[0], parts[-1]
            self.full[name.lower()] = tk_id
            self.initial_last[f"{first[0]}. {last}".lower()] = tk_id
            self.last.setdefault(last.lower(), []).append(tk_id)
            self.initials.setdefault((first[0] + last[0]).upper(), []).append(tk_id)

    def resolve(self, name: str) -> str | None:
        n = _TITLE.sub("", name.strip()).strip(".,;:").lower()
        if n in self.full:
            return self.full[n]
        if n in self.initial_last:
            return self.initial_last[n]
        if n in self.last and len(self.last[n]) == 1:
            return self.last[n][0]
        return None


def mentions(text: str, roster: Roster | None = None, doc: Any = None) -> list[Mention]:
    doc = doc if doc is not None else nlp()(text)
    out: list[Mention] = []
    taken: list[tuple[int, int]] = []
    for e in doc.ents:
        if e.label_ in ("PERSON", "ORG", "GPE", "DATE"):
            tk = roster.resolve(e.text) if roster and e.label_ in ("PERSON", "ORG") else None
            out.append(Mention(e.text, e.start_char, e.end_char, "PERSON" if tk else e.label_, tk))
            taken.append((e.start_char, e.end_char))
    if roster:
        # spaCy misses bare surnames and initials in terse entries; the roster catches those it knows.
        # Initials only count in mixed-case text and never when they are a common abbreviation.
        shouting = sum(c.isupper() for c in text) > 0.6 * max(1, sum(c.isalpha() for c in text))
        for m in re.finditer(r"\b([A-Z][a-z]+|[A-Z]{2})\b", text):
            if any(a <= m.start() < b for a, b in taken):
                continue
            tok = m.group(1)
            if len(tok) == 2 and (shouting or tok in NOT_INITIALS):
                continue
            tk = (
                roster.resolve(tok)
                if len(tok) > 2
                else (roster.initials.get(tok, [None])[0] if len(roster.initials.get(tok, [])) == 1 else None)
            )
            if tk:
                out.append(Mention(tok, m.start(), m.end(), "PERSON", tk))
    return sorted(out, key=lambda x: x.start)


def pseudonymize(text: str, ms: list[Mention], names: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """Replace person mentions with PERSON_n placeholders, stable across one invoice via ``names``."""
    names = {} if names is None else names
    out, last = [], 0
    for m in ms:
        if m.label != "PERSON":
            continue
        # a lone capitalised word ("Bates", "Drft") is often not a name: only roster matches, full names or titled names count
        if m.timekeeper_id is None and len(m.text.split()) < 2:
            continue
        key = m.timekeeper_id or m.text.lower()
        if key not in names:
            names[key] = f"PERSON_{len(names) + 1}"
        out.append(text[last : m.start])
        out.append(names[key])
        last = m.end
    out.append(text[last:])
    return "".join(out), names
