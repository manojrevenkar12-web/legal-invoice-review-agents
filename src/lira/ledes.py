"""LEDES 1998B: read, write and check the e-billing file law firms submit.

The file is plain text: the line ``LEDES1998B[]``, a header line naming the 24 fields separated by ``|``,
then one line per invoice line item. Every line ends with ``[]``. Invoice-level fields repeat on each line.
The checks follow the rules e-billing platforms apply (Collaborati's published 1998B specification):
required fields present, totals that add up, units above zero, unique line numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from lira.domain import Invoice, LineItem, LineType, money

FIELDS: tuple[str, ...] = (
    "INVOICE_DATE",
    "INVOICE_NUMBER",
    "CLIENT_ID",
    "LAW_FIRM_MATTER_ID",
    "INVOICE_TOTAL",
    "BILLING_START_DATE",
    "BILLING_END_DATE",
    "INVOICE_DESCRIPTION",
    "LINE_ITEM_NUMBER",
    "EXP/FEE/INV_ADJ_TYPE",
    "LINE_ITEM_NUMBER_OF_UNITS",
    "LINE_ITEM_ADJUSTMENT_AMOUNT",
    "LINE_ITEM_TOTAL",
    "LINE_ITEM_DATE",
    "LINE_ITEM_TASK_CODE",
    "LINE_ITEM_EXPENSE_CODE",
    "LINE_ITEM_ACTIVITY_CODE",
    "TIMEKEEPER_ID",
    "LINE_ITEM_DESCRIPTION",
    "LAW_FIRM_ID",
    "LINE_ITEM_UNIT_COST",
    "TIMEKEEPER_NAME",
    "TIMEKEEPER_CLASSIFICATION",
    "CLIENT_MATTER_ID",
)
MARKER = "LEDES1998B[]"
EOL = "[]"


@dataclass(frozen=True)
class Issue:
    code: str  # machine-readable: "missing_field", "line_total", "invoice_total", ...
    message: str
    line_no: str | None = None
    fatal: bool = True


class LedesError(ValueError):
    def __init__(self, issues: list[Issue]):
        super().__init__("; ".join(i.message for i in issues[:5]))
        self.issues = issues


def _date(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def _fmt_date(d: date | None) -> str:
    return d.strftime("%Y%m%d") if d else ""


def _dec(s: str) -> Decimal:
    return Decimal(s.strip() or "0")


def _fmt_dec(x: Decimal, places: int = 2) -> str:
    return f"{x:.{places}f}"


def write(inv: Invoice) -> str:
    """Serialise an invoice as LEDES 1998B."""
    out = [MARKER, "|".join(FIELDS) + EOL]
    for li in inv.lines:
        row = {
            "INVOICE_DATE": _fmt_date(inv.date),
            "INVOICE_NUMBER": inv.number,
            "CLIENT_ID": inv.client_id,
            "LAW_FIRM_MATTER_ID": inv.law_firm_matter_id,
            "INVOICE_TOTAL": _fmt_dec(inv.total),
            "BILLING_START_DATE": _fmt_date(inv.period_start),
            "BILLING_END_DATE": _fmt_date(inv.period_end),
            "INVOICE_DESCRIPTION": inv.description,
            "LINE_ITEM_NUMBER": li.line_no,
            "EXP/FEE/INV_ADJ_TYPE": li.type.value,
            "LINE_ITEM_NUMBER_OF_UNITS": _fmt_dec(li.units, 2),
            "LINE_ITEM_ADJUSTMENT_AMOUNT": _fmt_dec(li.adjustment) if li.adjustment else "",
            "LINE_ITEM_TOTAL": _fmt_dec(li.total),
            "LINE_ITEM_DATE": _fmt_date(li.date),
            "LINE_ITEM_TASK_CODE": li.task_code,
            "LINE_ITEM_EXPENSE_CODE": li.expense_code,
            "LINE_ITEM_ACTIVITY_CODE": li.activity_code,
            "TIMEKEEPER_ID": li.timekeeper_id,
            "LINE_ITEM_DESCRIPTION": li.description,
            "LAW_FIRM_ID": inv.law_firm_id,
            "LINE_ITEM_UNIT_COST": _fmt_dec(li.unit_cost, 2),
            "TIMEKEEPER_NAME": li.timekeeper_name,
            "TIMEKEEPER_CLASSIFICATION": li.timekeeper_class,
            "CLIENT_MATTER_ID": inv.client_matter_id,
        }
        out.append("|".join(_clean(row[f]) for f in FIELDS) + EOL)
    return "\n".join(out) + "\n"


def _clean(v: str) -> str:
    # the delimiter and the terminator cannot appear inside a value
    return v.replace("|", "/").replace("[]", "").replace("\n", " ").strip()


def parse(text: str) -> Invoice:
    """Parse one invoice. Raises LedesError with every fatal issue found (not only the first)."""
    issues: list[Issue] = []
    rows = [r.strip() for r in text.replace("\r\n", "\n").split("\n") if r.strip()]
    if not rows or rows[0] != MARKER:
        raise LedesError([Issue("marker", "first line must be LEDES1998B[]")])
    if len(rows) < 3:
        raise LedesError([Issue("empty", "no line items")])
    header = rows[1].removesuffix(EOL).split("|")
    if tuple(header) != FIELDS:
        missing = [f for f in FIELDS if f not in header]
        raise LedesError([Issue("header", f"header does not match LEDES 1998B (missing: {', '.join(missing) or 'order'})")])
    records: list[dict[str, str]] = []
    for n, raw in enumerate(rows[2:], start=3):
        if not raw.endswith(EOL):
            issues.append(Issue("terminator", f"file line {n} does not end with []"))
            continue
        values = raw.removesuffix(EOL).split("|")
        if len(values) != len(FIELDS):
            issues.append(Issue("field_count", f"file line {n} has {len(values)} fields, expected {len(FIELDS)}"))
            continue
        records.append(dict(zip(FIELDS, values, strict=True)))
    if issues:
        raise LedesError(issues)
    first = records[0]
    for key in ("INVOICE_NUMBER", "INVOICE_DATE", "CLIENT_ID", "INVOICE_TOTAL", "CLIENT_MATTER_ID"):
        if any(r[key] != first[key] for r in records):
            issues.append(Issue("invoice_field_varies", f"{key} differs between lines of one invoice"))
    lines: list[LineItem] = []
    for r in records:
        ln = r["LINE_ITEM_NUMBER"].strip()
        try:
            li = _line(r, ln)
        except (ValueError, InvalidOperation) as e:
            issues.append(Issue("bad_value", f"line {ln}: {e}", ln))
            continue
        lines.append(li)
    try:
        inv = Invoice(
            number=first["INVOICE_NUMBER"].strip(),
            date=_date(first["INVOICE_DATE"]),
            client_id=first["CLIENT_ID"].strip(),
            law_firm_id=first["LAW_FIRM_ID"].strip(),
            law_firm_matter_id=first["LAW_FIRM_MATTER_ID"].strip(),
            client_matter_id=first["CLIENT_MATTER_ID"].strip(),
            total=money(_dec(first["INVOICE_TOTAL"])),
            period_start=_date(first["BILLING_START_DATE"]) if first["BILLING_START_DATE"].strip() else None,
            period_end=_date(first["BILLING_END_DATE"]) if first["BILLING_END_DATE"].strip() else None,
            description=first["INVOICE_DESCRIPTION"].strip(),
            lines=tuple(lines),
        )
    except (ValueError, InvalidOperation) as e:
        issues.append(Issue("bad_value", f"invoice header: {e}"))
    if issues:
        raise LedesError(issues)
    return inv


def _line(r: dict[str, str], ln: str) -> LineItem:
    if not ln:
        raise ValueError("LINE_ITEM_NUMBER is required")
    t = r["EXP/FEE/INV_ADJ_TYPE"].strip()
    if t not in {x.value for x in LineType}:
        raise ValueError(f"EXP/FEE/INV_ADJ_TYPE must be F, E, IF or IE, got {t!r}")
    return LineItem(
        line_no=ln,
        type=LineType(t),
        date=_date(r["LINE_ITEM_DATE"]),
        units=_dec(r["LINE_ITEM_NUMBER_OF_UNITS"]),
        unit_cost=_dec(r["LINE_ITEM_UNIT_COST"]),
        adjustment=_dec(r["LINE_ITEM_ADJUSTMENT_AMOUNT"]),
        total=_dec(r["LINE_ITEM_TOTAL"]),
        task_code=r["LINE_ITEM_TASK_CODE"].strip(),
        expense_code=r["LINE_ITEM_EXPENSE_CODE"].strip(),
        activity_code=r["LINE_ITEM_ACTIVITY_CODE"].strip(),
        timekeeper_id=r["TIMEKEEPER_ID"].strip(),
        timekeeper_name=r["TIMEKEEPER_NAME"].strip(),
        timekeeper_class=r["TIMEKEEPER_CLASSIFICATION"].strip(),
        description=r["LINE_ITEM_DESCRIPTION"].strip(),
    )


def check(inv: Invoice) -> list[Issue]:
    """Arithmetic and required-field checks on a parsed invoice."""
    issues: list[Issue] = []
    seen: set[str] = set()
    for li in inv.lines:
        if li.line_no in seen:
            issues.append(Issue("duplicate_line", f"line number {li.line_no} is used twice", li.line_no))
        seen.add(li.line_no)
        adj_line = li.type in (LineType.INV_ADJ_FEE, LineType.INV_ADJ_EXPENSE)
        if li.units <= 0 and not adj_line:
            issues.append(Issue("units", f"line {li.line_no}: units must be greater than 0", li.line_no))
        if li.unit_cost == 0 and not adj_line:
            issues.append(Issue("unit_cost", f"line {li.line_no}: unit cost is zero", li.line_no))
        expected = money(li.unit_cost * li.units + li.adjustment)
        if money(li.total) != expected:
            issues.append(
                Issue("line_total", f"line {li.line_no}: total {li.total} is not units x cost + adjustment = {expected}", li.line_no)
            )
        if li.is_fee and not li.task_code:
            issues.append(Issue("task_code", f"line {li.line_no}: fee lines need a task code", li.line_no))
        if li.is_fee and not li.timekeeper_id:
            issues.append(Issue("timekeeper", f"line {li.line_no}: fee lines need a timekeeper", li.line_no))
        if li.is_expense and not li.expense_code:
            issues.append(Issue("expense_code", f"line {li.line_no}: expense lines need an expense code", li.line_no))
        if inv.period_start and inv.period_end and not (inv.period_start <= li.date <= inv.period_end):
            issues.append(Issue("outside_period", f"line {li.line_no}: dated {li.date} outside the billing period", li.line_no, fatal=False))
    line_sum = money(sum((li.total for li in inv.lines), Decimal("0")))
    if line_sum != money(inv.total):
        issues.append(Issue("invoice_total", f"invoice total {inv.total} is not the sum of its lines {line_sum}"))
    return issues
