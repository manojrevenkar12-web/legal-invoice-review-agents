"""Read every line of an invoice with the classical models: the cheap pass that decides what a language model needs to read."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from lira.domain import Firm, Invoice
from lira.guard.screen import screen
from lira.nlp import entities as E
from lira.nlp.models import NlpModels
from lira.nlp.text import normalize, task_segments
from lira.review.engine import LineSignals

TRAVEL = re.compile(r"^\s*(?:[LA]\d{3}/[LA]\d{3}\s+)?(?:return\s+)?(?:travel|trvl)\b|\btravel (?:to|from)\b", re.I)
EVENT = re.compile(
    r"\b(attend|attended|defend|defended|appear|appearance)\b.*?\b(deposition|depo|hearing|trial|conference before|mediation)\b"
    r"|\b(prepare for and attend)\b",
    re.I,
)
CONFERENCE = re.compile(r"\b(conference|conf|meet|meeting|discuss|discussed|strategy session|confer|team meeting)\b", re.I)
EXTERNAL = re.compile(r"\b(client|opposing counsel|expert|vendor|court reporter|mediator|judge|witness|in-house)\b", re.I)


@dataclass
class LineAnalysis:
    line_no: str
    normalized: str
    tasks: int
    p: dict[str, float]  # block_billing / vague_description / clerical_work
    task_code_pred: str
    task_code_conf: float
    people: list[dict[str, Any]] = field(default_factory=list)  # {text, at_firm, timekeeper_id}
    colleagues: list[str] = field(default_factory=list)
    conference: bool = False
    travel: bool = False
    event_kind: str | None = None
    event_key: str | None = None
    injection: str | None = None
    hours_z: float = 0.0
    topic: list[float] = field(default_factory=list)

    def signals(self) -> LineSignals:
        return LineSignals(travel=self.travel, event_kind=self.event_kind, event_key=self.event_key)


def analyze(inv: Invoice, firm: Firm, models: NlpModels) -> dict[str, LineAnalysis]:
    fees = [li for li in inv.lines if li.is_fee]
    roster = E.Roster([(t.id, t.name) for t in firm.timekeepers])
    rows = [
        {
            "description": li.description,
            "timekeeper_class": li.timekeeper_class,
            "activity_code": li.activity_code,
            "units": float(li.units),
        }
        for li in fees
    ]
    probs = models.judgement_probs(rows)
    codes, conf = models.task_codes([li.description for li in fees])
    topics = models.topic_mix([li.description for li in fees]) if fees else np.zeros((0, 0))
    docs = list(E.nlp().pipe([li.description for li in fees], batch_size=64)) if fees else []
    out: dict[str, LineAnalysis] = {}
    for i, li in enumerate(fees):
        text = li.description
        ms = E.mentions(text, roster, docs[i])
        people = [
            {
                "text": m.text,
                "start": m.start,
                "end": m.end,
                "timekeeper_id": m.timekeeper_id,
                "at_firm": m.timekeeper_id is not None and m.timekeeper_id != li.timekeeper_id,
            }
            for m in ms
            if m.label == "PERSON" and (m.timekeeper_id is not None or len(m.text.split()) >= 2)
        ]
        colleagues = sorted({str(p["timekeeper_id"]) for p in people if p["at_firm"]})
        conference = (
            (li.activity_code == "A105" or bool(CONFERENCE.search(normalize(text)))) and bool(colleagues) and not EXTERNAL.search(text)
        )
        travel = bool(TRAVEL.search(text))
        kind, key = _event(li, text, ms)
        inj = screen(text).injection
        a = LineAnalysis(
            line_no=li.line_no,
            normalized=normalize(text),
            tasks=len(task_segments(text)),
            p={q: float(v[i]) for q, v in probs.items()},
            task_code_pred=codes[i],
            task_code_conf=float(conf[i]),
            people=people,
            colleagues=colleagues,
            conference=conference,
            travel=travel,
            event_kind=kind,
            event_key=key,
            injection=inj,
            hours_z=models.hours_z(li.task_code, li.activity_code, li.timekeeper_class, float(li.units)),
            topic=[float(x) for x in topics[i]] if len(topics) else [],
        )
        out[li.line_no] = a
    return out


def _event(li: Any, text: str, ms: list[E.Mention]) -> tuple[str | None, str | None]:
    """Attendance at a deposition or hearing, keyed so that colleagues attending the same event share a key."""
    lo = normalize(text)
    if not (EVENT.search(text) or EVENT.search(lo) or li.activity_code == "A109"):
        return None, None
    if re.search(r"deposition|depo\b", lo):
        kind = "deposition"
    elif re.search(r"hearing|trial|conference before|pretrial|mediation", lo):
        kind = "hearing"
    else:
        return None, None
    if not re.search(r"\b(attend|defend|appear|prepare for and attend)", lo):
        return None, None
    who = next((m.text.split()[-1].lower() for m in ms if m.label == "PERSON"), "")
    if not who:
        m = re.search(r"(?:judge|justice|mediator|panel)\s+([a-z]+)", lo)
        who = m.group(1) if m else re.sub(r"[^a-z ]", "", lo)[:40]
    sub = next((w for w in ("deposition", "trial", "pretrial", "mediation", "conference", "hearing") if re.search(rf"\b{w}\b", lo)), kind)
    day = re.search(r"day\s*\(?(\d+)", lo)
    return kind, f"{li.date.isoformat()}|{kind}|{sub}{day.group(1) if day else ''}|{who}"
