"""The records the system works on: firms, timekeepers, matters, rate agreements, invoices and findings.

Money is Decimal throughout: an adjustment is a payment decision, so it must add up to the cent.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CENT = Decimal("0.01")


def money(x: Decimal | float | int | str) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------- reference data (from the ELM system)


class Timekeeper(Frozen):
    id: str
    firm_id: str
    name: str
    classification: Literal["PT", "AS", "OC", "LA", "OT"]  # LEDES classifications
    title: str  # "Partner", "Senior Associate", "First-Year Associate", "Paralegal", "Summer Associate", ...
    seniority: int  # higher is more senior; decides which attendee keeps a shared meeting


class Firm(Frozen):
    id: str
    name: str
    timekeepers: tuple[Timekeeper, ...]

    def timekeeper(self, tk_id: str) -> Timekeeper | None:
        return next((t for t in self.timekeepers if t.id == tk_id), None)


class RateAgreement(Frozen):
    tenant: str
    firm_id: str
    timekeeper_id: str
    rate: Decimal
    effective_from: date
    effective_to: date | None = None

    def covers(self, d: date) -> bool:
        return self.effective_from <= d and (self.effective_to is None or d <= self.effective_to)


class Matter(Frozen):
    id: str  # CLIENT_MATTER_ID
    tenant: str
    firm_id: str
    law_firm_matter_id: str
    name: str
    budget: Decimal
    opened: date
    authorized_timekeepers: tuple[str, ...]
    authorized_task_codes: tuple[str, ...]
    authorized_expense_codes: tuple[str, ...]


class Tenant(Frozen):
    id: str
    name: str
    client_id: str  # CLIENT_ID on the firm's LEDES file
    currency: str
    allowed_model_regions: tuple[str, ...] = ("eu-central-1",)


# ---------------------------------------------------------------- invoices (LEDES 1998B)


class LineType(StrEnum):
    FEE = "F"
    EXPENSE = "E"
    INV_ADJ_FEE = "IF"
    INV_ADJ_EXPENSE = "IE"


class LineItem(Frozen):
    line_no: str
    type: LineType
    date: date
    units: Decimal
    unit_cost: Decimal
    adjustment: Decimal = Decimal("0")
    total: Decimal
    task_code: str = ""
    expense_code: str = ""
    activity_code: str = ""
    timekeeper_id: str = ""
    timekeeper_name: str = ""
    timekeeper_class: str = ""
    description: str = ""

    @property
    def is_fee(self) -> bool:
        return self.type == LineType.FEE

    @property
    def is_expense(self) -> bool:
        return self.type == LineType.EXPENSE


class Invoice(Frozen):
    number: str
    date: date
    client_id: str
    law_firm_id: str
    law_firm_matter_id: str = ""
    client_matter_id: str
    total: Decimal
    period_start: date | None = None
    period_end: date | None = None
    description: str = ""
    lines: tuple[LineItem, ...]

    def line(self, line_no: str) -> LineItem:
        for li in self.lines:
            if li.line_no == line_no:
                return li
        raise KeyError(line_no)


# ---------------------------------------------------------------- findings


class FindingKind(StrEnum):
    # validation (the file itself)
    FORMAT = "format_error"
    UNAUTHORIZED = "unauthorized"
    # measurable guideline rules: code decides
    RATE = "rate_cap"
    INCREMENT = "billing_increment"
    DAILY_HOURS = "max_daily_hours"
    TRAVEL = "travel_time"
    ROLE = "nonbillable_role"
    EXPENSE = "nonreimbursable_expense"
    EXPENSE_CAP = "expense_unit_cap"
    DEADLINE = "submission_deadline"
    ATTENDEES = "max_attendees"
    RESEARCH = "research_cap"
    BUDGET = "budget"
    # judgement rules: a model proposes, a person decides
    BLOCK = "block_billing"
    VAGUE = "vague_description"
    CLERICAL = "clerical_work"
    CONFERENCE = "internal_conference"
    # signals without a rule behind them
    TASK_CODE = "task_code_mismatch"
    UNUSUAL = "unusual_time"
    INJECTION = "prompt_injection"


JUDGEMENT_KINDS = frozenset({FindingKind.BLOCK, FindingKind.VAGUE, FindingKind.CLERICAL, FindingKind.CONFERENCE})
INVOICE_LEVEL_KINDS = frozenset({FindingKind.DEADLINE, FindingKind.BUDGET})


class ClauseRef(Frozen):
    document: str  # "<tenant>/guidelines@v2"
    clause_id: str  # "6.1"
    quote: str  # verbatim from the clause


class Evidence(Frozen):
    source: Literal["line", "rule", "nlp", "model", "guard", "cross_line"]
    text: str
    start: int | None = None  # character span in the line description, when the evidence is a quote from it
    end: int | None = None


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    invoice: str
    line_no: str | None
    kind: FindingKind
    source: Literal["validation", "rule", "nlp", "model", "guard", "cross_line"]
    decided_by: Literal["rule", "person"]
    action: Literal["reject", "reduce", "flag"]
    adjustment: Decimal = Decimal("0")  # <= 0: the amount taken off the line
    clause: ClauseRef | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float | None = None
    severity: float = 0.0
    rationale: str = ""
    status: Literal["auto_applied", "proposed", "accepted", "rejected", "withdrawn"] = "proposed"
    explanation: str = ""  # what the law firm reads (review.explain)
    decision: dict[str, Any] | None = None  # the reviewer's decision, when there is one
