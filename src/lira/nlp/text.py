"""Normalise time-entry text and split it into tasks.

Firms abbreviate ("TC w/ OC re depo"), prefix codes ("L330/A104"), shout in capitals, and join several tasks in one
entry with semicolons, slashes or "and". Normalising first makes every downstream model see one vocabulary, and it
blunts the effect of a firm changing its timekeeping system (see eval.drift).
"""

from __future__ import annotations

import re

ABBREVIATIONS: dict[str, str] = {
    "tc": "telephone conference",
    "tel": "telephone",
    "w/": "with",
    "re": "regarding",
    "re:": "regarding",
    "depo": "deposition",
    "depos": "depositions",
    "rev": "review",
    "rev/analyze": "review and analyze",
    "drft": "draft",
    "prep": "prepare",
    "rogs": "interrogatories",
    "rfps": "requests for production",
    "sj": "summary judgment",
    "conf": "conference",
    "mtn": "motion",
    "atty": "attorney",
    "docs": "documents",
    "memo": "memorandum",
    "&": "and",
    "oc": "opposing counsel",
    "corr": "correspondence",
    "anal": "analysis",
    "mtg": "meeting",
    "emails": "emails",
    "msj": "motion for summary judgment",
    "tel.": "telephone",
}
_DANGLING = frozenset({"for", "to", "with", "on", "in", "of", "and", "regarding", "re", "the", "a"})
_CODE_PREFIX = re.compile(r"^\s*[LAE]\d{3}(?:/[LAE]\d{3})?\s*[-:]?\s*", re.I)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'./&-]*|\d+(?:\.\d+)?|&")

TASK_VERBS = frozenset(
    [
        "draft",
        "review",
        "prepare",
        "attend",
        "research",
        "analyze",
        "analyse",
        "revise",
        "interview",
        "coordinate",
        "examine",
        "evaluate",
        "summarize",
        "summarise",
        "cite-check",
        "defend",
        "call",
        "email",
        "telephone",
        "confer",
        "conference",
        "meet",
        "discuss",
        "strategize",
        "update",
        "file",
        "organize",
        "organise",
        "investigate",
        "travel",
        "confirm",
        "identify",
        "negotiate",
        "outline",
        "edit",
        "finalize",
        "finalise",
        "correspond",
        "calendar",
        "schedule",
        "scan",
        "copy",
        "assemble",
        "arrange",
        "book",
        "rename",
        "bates",
        "second-level",
        "videoconference",
        "meet-and-confer",
        "advise",
        "brief",
        "instruct",
        "compile",
        "collect",
        "circulate",
        "respond",
        "reply",
        "argue",
        "appear",
        "present",
        "prepared",
        "drafted",
        "reviewed",
        "attended",
        "analyzed",
        "revised",
    ]
)


def normalize(text: str) -> str:
    """Lower-case, strip code prefixes, expand abbreviations. Keeps word order; drops nothing meaningful."""
    t = _CODE_PREFIX.sub("", text.strip())
    out: list[str] = []
    for tok in re.findall(r"\S+", t):
        core = tok.lower().strip(",.;:()")
        trail = tok[len(tok.rstrip(",.;:")) :]
        if core in ABBREVIATIONS:
            out.append(ABBREVIATIONS[core] + (trail if trail in (";", ",") else ""))
        else:
            out.append(tok.lower())
    return " ".join(out)


def segments(text: str) -> list[str]:
    """Split an entry into the tasks it describes.

    Hard separators (";", " / ", ". ") always split. "and"/"," split only when the next words start a new task
    ("draft motion and review production" is two; "review and analyze responses" and "prepare for and attend
    deposition" are one, because the part before "and" has no object of its own).
    """
    t = normalize(text)
    out: list[str] = []
    for part in re.split(r";|\s/\s|\.\s+(?=[a-z])", t):
        part = part.strip(" .,")
        if not part:
            continue
        pieces = re.split(r"(,\s*(?:and\s+)?|\s+and\s+)", part)
        current = pieces[0]
        for i in range(1, len(pieces), 2):
            sep, nxt = pieces[i], pieces[i + 1] if i + 1 < len(pieces) else ""
            words = current.split()
            first = nxt.split()[0] if nxt.split() else ""
            has_object = len(words) >= 2 and words[-1] not in _DANGLING
            if first in TASK_VERBS and has_object and len(nxt.split()) >= 2:
                out.append(current.strip())
                current = nxt
            else:
                current = current + sep + nxt
        out.append(current.strip())
    return [s for s in out if s]


def task_segments(text: str) -> list[str]:
    """Segments that start with a task verb: the number of distinct tasks billed in one entry."""
    return [s for s in segments(text) if s.split() and s.split()[0].strip(".,") in TASK_VERBS]


def tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]
