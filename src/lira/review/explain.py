"""What the law firm reads for each adjustment: built from the entry, the numbers and the client's own clause.

The text is assembled by code, not written by a model, so it can only say what the finding rests on; the
LLM-as-judge in eval.judge grades these explanations like any other output.
"""

from __future__ import annotations

from decimal import Decimal

from lira.domain import Finding, FindingKind, LineItem

LEAD = {
    FindingKind.BLOCK: "combines several tasks in one time entry",
    FindingKind.VAGUE: "does not describe the work specifically enough to assess it",
    FindingKind.CLERICAL: "describes administrative work, which the guidelines treat as overhead",
    FindingKind.CONFERENCE: "records an internal conference between timekeepers of the firm",
    FindingKind.RATE: "is billed above the timekeeper's approved rate",
    FindingKind.INCREMENT: "is not recorded in the required time increment",
    FindingKind.DAILY_HOURS: "takes the timekeeper's hours for the day above the daily limit",
    FindingKind.TRAVEL: "bills travel time above what the guidelines allow",
    FindingKind.ROLE: "is recorded by a timekeeper whose time is not billable",
    FindingKind.EXPENSE: "is an expense the guidelines do not reimburse",
    FindingKind.EXPENSE_CAP: "is charged above the per-unit cap",
    FindingKind.ATTENDEES: "is one more attendance at the same event than the guidelines pay for",
    FindingKind.RESEARCH: "takes research for the month above the approved limit",
    FindingKind.UNAUTHORIZED: "is recorded by a timekeeper not authorised on this matter",
}


def explain(f: Finding, li: LineItem | None, currency: str) -> str:
    if li is None:
        clause = f' Clause {f.clause.clause_id}: "{f.clause.quote}"' if f.clause else ""
        return f"{f.rationale}{clause}"
    if f.kind in (FindingKind.TASK_CODE, FindingKind.UNUSUAL, FindingKind.INJECTION, FindingKind.FORMAT):
        return f"Line {li.line_no} ({li.date:%d %b %Y}): {f.rationale}"
    what = LEAD.get(f.kind, f.kind.value.replace("_", " "))
    hours = f"{li.units} h, " if li.is_fee else ""
    quote = next((e.text for e in f.evidence if e.source in ("line", "model") and e.text), "")
    said = (
        f' ("{quote}")'
        if quote and len(quote) < 160 and f.kind in (FindingKind.BLOCK, FindingKind.VAGUE, FindingKind.CLERICAL, FindingKind.CONFERENCE)
        else ""
    )
    clause = f' Clause {f.clause.clause_id} of the billing guidelines: "{f.clause.quote}"' if f.clause else ""
    amount = -f.adjustment if f.adjustment < 0 else Decimal("0")
    outcome = (
        f"The line is not payable ({currency} {amount:,.2f})."
        if f.action == "reject"
        else f"The line is reduced by {currency} {amount:,.2f}."
        if amount
        else "No amount is adjusted; please correct the entry."
    )
    return f"Line {li.line_no} ({hours}{li.date:%d %b %Y}) {what}{said}.{clause} {outcome}"
