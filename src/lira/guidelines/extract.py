"""A deterministic rule extractor: keyword and number patterns over each clause.

It runs next to the model-based compiler as a cross-check (legal operations sees where the two disagree), and it
is what the simulated model uses to answer the compile prompt. It is precise on the phrasings it knows and
silent on the rest, which is exactly why the model is the primary extractor.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from lira.guidelines.documents import Clause
from lira.utbms import EXPENSE_CODES

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
             "eleven": 11, "twelve": 12, "fourteen": 14, "fifteen": 15, "twenty": 20, "thirty": 30, "forty-five": 45,
             "sixty": 60, "ninety": 90}
NEG = re.compile(r"\b(not (?:be )?(?:billable|bill(?:ed)?|paid|pay|charged|reimburs\w*|accepted|compensated|compensable|chargeable)|"
                 r"will not (?:pay|be paid|reimburse|accept)|non-?billable|must not be charged|may not be charged|no charge|overheads?|unbillable)\b", re.I)
ROLES = {"summer associate": r"summer associates?", "law clerk": r"law clerks?", "first-year associate": r"first[- ]year associates?",
         "trainee": r"trainees?|trainee solicitors?", "paralegal": r"paralegals?"}


def _sentence(text: str, pattern: str) -> str:
    """The sentence of the clause that matches pattern (the quote a rule cites)."""
    for s in re.split(r"(?<=[.;])\s+(?=[A-Z(])", text):
        if re.search(pattern, s, re.I):
            return s.strip()
    return text.strip()


def _number(s: str) -> Decimal | None:
    s = s.lower()
    if s in NUM_WORDS:
        return Decimal(NUM_WORDS[s])
    try:
        return Decimal(s)
    except Exception:
        return None


def extract(clauses: list[Clause]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    def add(kind: str, c: Clause, quote_pattern: str, **params: Any) -> None:
        key = (kind, params.get("event"), params.get("code"))
        if key in seen:
            return
        seen.add(key)
        rules.append({"kind": kind, "clause_id": c.id, "quote": _sentence(c.text, quote_pattern), **params})

    for c in clauses:
        t = c.text
        lo = t.lower()
        neg = bool(NEG.search(t))
        # rates
        if re.search(r"\brates?\b", lo) and re.search(r"\b(approved|agreed)\b", lo) and re.search(r"increase|only|fixed", lo):
            add("rate_cap", c, r"rates?")
        # roles whose time is not billable
        if neg:
            roles = [r for r, pat in ROLES.items() if re.search(pat, lo)]
            if roles and "staff matters" not in lo:
                add("nonbillable_role", c, "|".join(ROLES[r] for r in roles), roles=roles)
        # attendance caps
        m = re.search(r"(?:no more than|at most|limited to|maximum of|only)\s+(\w+(?:-\w+)?)(?:\s*\((\d+)\))?\s+(?:timekeepers?|attorneys?|fee earners?|lawyers?)", lo)
        if m:
            n = _number(m.group(2) or m.group(1))
            for ev, pat in (("deposition", r"deposition"), ("hearing", r"hearing|court appearance|trial")):
                if n is not None and re.search(pat, lo):
                    add("max_attendees", c, pat, event=ev, max_timekeepers=int(n))
        else:
            m = re.search(r"attendance at an? (deposition|hearing)[^.;]*?limited to (\w+)", lo)
            if m:
                n = _number(m.group(2))
                if n is not None:
                    add("max_attendees", c, "attendance", event=m.group(1), max_timekeepers=int(n))
        # billing increment
        if re.search(r"increments?|units? of|tenth", lo) and re.search(r"\b0?\.1\b|one-tenth|tenth-of-an-hour|six minutes", lo):
            add("billing_increment", c, r"increment|units? of|tenth", hours=Decimal("0.1"))
        # block billing
        if "block" in lo and "bill" in lo:
            pct = re.search(r"reduced? by (\d{1,3})\s?%", lo) or re.search(r"reduce the entry by (\d{1,3})\s?%", lo)
            if pct:
                add("block_billing", c, r"block", action="reduce", reduce_factor=Decimal(pct.group(1)) / 100)
            else:
                add("block_billing", c, r"block|combin", action="reject")
        # vague descriptions
        if re.search(r"\b(vague|generic)\b", lo) and re.search(r"reject|not be paid|will not", lo):
            add("vague_description", c, r"vague|generic")
        # daily hours
        m = re.search(r"more than (\d{1,2}) hours[^.;]*?(?:in a single day|on any one day|per day|a day|in any day)", lo)
        if m:
            add("max_daily_hours", c, r"hours", hours=Decimal(m.group(1)))
        # clerical
        if re.search(r"\b(clerical|administrative|secretarial)\b", lo) and neg:
            add("clerical_work", c, r"clerical|administrative|secretarial")
        # internal conferences
        if re.search(r"\b(internal(?:ly)?|intra-firm)\b", lo) and re.search(r"conferences?|meetings?|confer", lo):
            if re.search(r"only (?:the most senior|one)|most senior", lo):
                add("internal_conference", c, r"internal|intra-firm", policy="one_attendee")
            elif neg:
                add("internal_conference", c, r"internal|intra-firm", policy="not_billable")
        # research cap
        m = re.search(r"research[^.;]*?(?:exceeding|above|of more than|more than)\s+(\d{1,2})\s+hours", lo)
        if m and re.search(r"month", lo):
            add("research_cap", c, r"research", hours_per_month=Decimal(m.group(1)))
        # travel time
        if re.search(r"travel time|non-working travel", lo):
            pct = re.search(r"(\d{1,3})\s?%", lo)
            if pct:
                add("travel_time", c, r"travel", rate_factor=Decimal(pct.group(1)) / 100)
            elif neg:
                add("travel_time", c, r"travel", rate_factor=Decimal("0"))
        # expenses not reimbursed
        if re.search(r"reimburs", lo) and neg:
            codes = [code for code, name in EXPENSE_CODES.items()
                     if code not in ("E123", "E124") and re.search(rf"\b{re.escape(name.lower())}\b", lo)]
            if codes:
                add("nonreimbursable_expense", c, r"reimburs|overhead", codes=codes)
        # copying cap
        m = re.search(r"(?:cop(?:y|ying)|photocopy(?:ing)?)[^.;]*?(?:£|\$|eur\s?|€)\s?(\d+\.\d{2})\s+per page", lo)
        if m:
            add("expense_unit_cap", c, r"per page", code="E101", max_unit_cost=Decimal(m.group(1)))
        # submission deadline
        m = re.search(r"invoices?[^.;]*?(?:within|more than)\s+(\d{1,3})\s+days", lo)
        if m:
            add("submission_deadline", c, r"days", days=int(m.group(1)))
        # budget
        if "budget" in lo and re.search(r"approv", lo) and re.search(r"excess|beyond|over", lo):
            add("budget", c, r"budget")
    return rules
