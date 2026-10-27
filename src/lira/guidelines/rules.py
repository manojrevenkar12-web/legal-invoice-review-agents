"""The checkable form of a client's billing guidelines.

A RuleSet is what the guideline compiler proposes and what legal operations approves once per guideline
version. Every rule carries the clause it came from and a verbatim quote; ``verify`` checks mechanically that
the quote is in the clause and that the rule's values are stated in the quote, so a rule cannot be invented.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from lira.utbms import EXPENSE_CODES


class _Rule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    clause_id: str
    quote: str


class BillingIncrement(_Rule):
    kind: Literal["billing_increment"] = "billing_increment"
    hours: Decimal


class MaxDailyHours(_Rule):
    kind: Literal["max_daily_hours"] = "max_daily_hours"
    hours: Decimal


class TravelTime(_Rule):
    kind: Literal["travel_time"] = "travel_time"
    rate_factor: Decimal  # 0 means travel time is not billable


class InternalConference(_Rule):
    kind: Literal["internal_conference"] = "internal_conference"
    policy: Literal["not_billable", "one_attendee"]


class BlockBilling(_Rule):
    kind: Literal["block_billing"] = "block_billing"
    action: Literal["reject", "reduce"]
    reduce_factor: Decimal | None = None  # share taken off when action is reduce


class VagueDescription(_Rule):
    kind: Literal["vague_description"] = "vague_description"
    action: Literal["reject"] = "reject"


class ClericalWork(_Rule):
    kind: Literal["clerical_work"] = "clerical_work"
    action: Literal["reject"] = "reject"


class NonbillableRoles(_Rule):
    kind: Literal["nonbillable_role"] = "nonbillable_role"
    roles: tuple[str, ...]


class NonreimbursableExpenses(_Rule):
    kind: Literal["nonreimbursable_expense"] = "nonreimbursable_expense"
    codes: tuple[str, ...]


class ExpenseUnitCap(_Rule):
    kind: Literal["expense_unit_cap"] = "expense_unit_cap"
    code: str
    max_unit_cost: Decimal


class SubmissionDeadline(_Rule):
    kind: Literal["submission_deadline"] = "submission_deadline"
    days: int


class MaxAttendees(_Rule):
    kind: Literal["max_attendees"] = "max_attendees"
    event: Literal["deposition", "hearing"]
    max_timekeepers: int


class ResearchCap(_Rule):
    kind: Literal["research_cap"] = "research_cap"
    hours_per_month: Decimal


class ApprovedRates(_Rule):
    kind: Literal["rate_cap"] = "rate_cap"


class BudgetApproval(_Rule):
    kind: Literal["budget"] = "budget"


Rule = Annotated[
    BillingIncrement
    | MaxDailyHours
    | TravelTime
    | InternalConference
    | BlockBilling
    | VagueDescription
    | ClericalWork
    | NonbillableRoles
    | NonreimbursableExpenses
    | ExpenseUnitCap
    | SubmissionDeadline
    | MaxAttendees
    | ResearchCap
    | ApprovedRates
    | BudgetApproval,
    Field(discriminator="kind"),
]

RULE_KINDS: tuple[str, ...] = (
    "billing_increment",
    "max_daily_hours",
    "travel_time",
    "internal_conference",
    "block_billing",
    "vague_description",
    "clerical_work",
    "nonbillable_role",
    "nonreimbursable_expense",
    "expense_unit_cap",
    "submission_deadline",
    "max_attendees",
    "research_cap",
    "rate_cap",
    "budget",
)


class RuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    version: int
    document: str  # "<tenant>/guidelines@v2"
    document_sha256: str
    effective_from: date
    rules: list[Rule]
    status: Literal["draft", "approved", "superseded"] = "draft"
    approved_by: str | None = None
    approved_at: datetime | None = None
    compiled_by: str = ""  # which extractor/model produced the draft

    def of(self, kind: str) -> list[Rule]:
        return [r for r in self.rules if r.kind == kind]

    def one(self, kind: str) -> Rule | None:
        found = self.of(kind)
        return found[0] if found else None

    def key(self, r: Rule) -> tuple[object, ...]:
        """Identity of a rule for diffs: kind plus its distinguishing parameter."""
        if isinstance(r, ExpenseUnitCap):
            return (r.kind, r.code)
        if isinstance(r, MaxAttendees):
            return (r.kind, r.event)
        return (r.kind,)


# ---------------------------------------------------------------- verification

_WORDS = {
    Decimal("0.1"): ["0.1", ".1", "one-tenth", "one tenth", "tenth", "six minutes", "6 minutes", "6-minute", "six-minute"],
    Decimal("0.25"): ["0.25", ".25", "quarter", "15 minutes", "fifteen minutes"],
    Decimal("0.5"): ["0.5", "50%", "50 %", "50 percent", "fifty percent", "half", "one-half"],
}
_NOT_BILLABLE = ["not bill", "not be bill", "non-billable", "nonbillable", "not billable", "not pay", "not be paid",
                 "will not be compensated", "no charge", "not chargeable", "not reimburs", "not compensable", "shall not be charged",
                 "not be charged", "may not be charged", "not accept", "unbillable"]
_NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
                 10: "ten", 11: "eleven", 12: "twelve", 14: "fourteen", 15: "fifteen", 20: "twenty", 30: "thirty",
                 45: "forty-five", 60: "sixty", 90: "ninety", 120: "one hundred twenty"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("’", "'").replace("–", "-").replace("—", "-")).strip().lower()


def number_forms(x: Decimal | int) -> list[str]:
    d = Decimal(str(x))
    forms = list(_WORDS.get(d, []))
    if d == d.to_integral_value():
        n = int(d)
        forms += [str(n), f"{n}.0", f"{n}.00"]
        if n in _NUMBER_WORDS:
            forms.append(_NUMBER_WORDS[n])
    else:
        s = format(d.normalize(), "f")
        forms += [s, s.lstrip("0"), f"{d:.2f}"]
        cents = d * 100
        if cents == cents.to_integral_value() and d < 1:
            forms += [f"{int(cents)} cents", f"{int(cents)} cent", f"{int(cents)}c"]
    return forms


def _has_number(quote: str, x: Decimal | int) -> bool:
    q = _norm(quote)
    for f in number_forms(x):
        f = f.lower()
        if re.fullmatch(r"[\d.]+", f):
            if re.search(rf"(?<![\d.]){re.escape(f)}(?![\d])", q):
                return True
        elif f in q:
            return True
    return False


def verify(rule: Rule, clause_text: str) -> list[str]:
    """Problems with a rule against the clause it cites; empty means it is supported by the text."""
    problems: list[str] = []
    quote, text = _norm(rule.quote), _norm(clause_text)
    if len(quote) < 12:
        problems.append("quote is too short to support a rule")
    if quote not in text:
        problems.append("quote is not verbatim in the cited clause")
        return problems
    q = rule.quote
    match rule:
        case BillingIncrement(hours=h) | MaxDailyHours(hours=h) | ResearchCap(hours_per_month=h):
            if not _has_number(q, h):
                problems.append(f"{h} is not stated in the quote")
        case TravelTime(rate_factor=f):
            ok = any(w in _norm(q) for w in _NOT_BILLABLE) if f == 0 else _has_number(q, f) or (
                f == Decimal("0.5") and "half" in _norm(q)
            )
            if not ok:
                problems.append(f"travel rate factor {f} is not stated in the quote")
        case BlockBilling(action="reduce", reduce_factor=f):
            if f is None or not _has_number(q, f):
                problems.append("the reduction for block billing is not stated in the quote")
        case NonbillableRoles(roles=roles):
            for role in roles:
                if _norm(role) not in _norm(q) and _norm(role).rstrip("s") not in _norm(q):
                    problems.append(f"role {role!r} is not named in the quote")
        case NonreimbursableExpenses(codes=codes):
            for c in codes:
                name = EXPENSE_CODES.get(c, "")
                if c.lower() not in _norm(q) and (not name or _norm(name) not in _norm(q)):
                    problems.append(f"expense {c} ({name}) is not named in the quote")
        case ExpenseUnitCap(code=c, max_unit_cost=m):
            name = EXPENSE_CODES.get(c, "")
            if c.lower() not in _norm(q) and _norm(name) not in _norm(q) and not (c == "E101" and ("cop" in _norm(q) or "photocop" in _norm(q))):
                problems.append(f"expense {c} is not named in the quote")
            if not _has_number(q, m):
                problems.append(f"cap {m} is not stated in the quote")
        case SubmissionDeadline(days=d):
            if not _has_number(q, d):
                problems.append(f"{d} days is not stated in the quote")
        case MaxAttendees(max_timekeepers=n, event=ev):
            if not _has_number(q, n) and not (n == 1 and re.search(r"\b(a single|only one|one)\b", _norm(q))):
                problems.append(f"{n} attendees is not stated in the quote")
            stems = {"deposition": ("deposition",), "hearing": ("hearing", "trial", "court appearance")}[ev]
            if not any(s in _norm(q) for s in stems):
                problems.append(f"{ev} is not named in the quote")
        case InternalConference(policy=p):
            if p == "not_billable" and not any(w in _norm(q) for w in _NOT_BILLABLE):
                problems.append("the quote does not say internal conferences are not billable")
        case _:
            pass
    return problems
