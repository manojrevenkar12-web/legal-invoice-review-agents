from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from lira import ledes
from lira.domain import Invoice, LineItem, LineType
from lira.guidelines.documents import parse
from lira.guidelines.extract import extract
from lira.guidelines.rules import BillingIncrement, ExpenseUnitCap, MaxAttendees, NonreimbursableExpenses, TravelTime, verify


def _inv(**kw: object) -> Invoice:
    li = LineItem(
        line_no="1",
        type=LineType.FEE,
        date=date(2026, 9, 2),
        units=Decimal("1.5"),
        unit_cost=Decimal("400"),
        total=Decimal("600.00"),
        task_code="L110",
        activity_code="A104",
        timekeeper_id="T1",
        timekeeper_name="A B",
        timekeeper_class="PT",
        description="Review | pipe [] and\nnewline",
    )
    base = dict(
        number="INV-1",
        date=date(2026, 10, 3),
        client_id="C",
        law_firm_id="f",
        client_matter_id="M1",
        total=Decimal("600.00"),
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        lines=(li,),
    )
    base.update(kw)
    return Invoice(**base)  # type: ignore[arg-type]


def test_ledes_round_trip_and_values_cannot_break_the_format():
    inv = _inv()
    text = ledes.write(inv)
    assert text.startswith("LEDES1998B[]\n") and all(line.endswith("[]") for line in text.strip().splitlines())
    back = ledes.parse(text)
    assert back.lines[0].description == "Review / pipe  and newline"
    assert back.total == Decimal("600.00") and ledes.check(back) == []


def test_ledes_reports_every_problem_not_only_the_first():
    text = ledes.write(_inv()).replace("|600.00|20260902", "|601.00|20260902")
    text = text.replace("LEDES1998B[]", "LEDES1998B[]", 1)
    inv = ledes.parse(text)
    codes = {i.code for i in ledes.check(inv)}
    assert {"line_total", "invoice_total"} <= codes
    with pytest.raises(ledes.LedesError) as e:
        ledes.parse("LEDES98[]\nfoo")
    assert e.value.issues[0].code == "marker"
    rows = text.strip().splitlines()
    bad = "\n".join([*rows[:-1], rows[-1].removesuffix("[]")])
    with pytest.raises(ledes.LedesError) as e2:
        ledes.parse(bad)
    assert [i.code for i in e2.value.issues] == ["terminator"]


GUIDE = """# Acme - Billing Guidelines

Version 1.

## 3. Time recording

3.1 Time must be recorded in increments of one-tenth of an hour (0.1). Minimum charges are not accepted.

3.2 Block billing is not permitted; block-billed entries will be reduced by 50%.
- this bullet belongs to 3.2

## Section 5: Travel

5.1 Non-working travel time may be billed at 50% of the approved rate.

(b) Attendance is noted.

## 6. Expenses

6.1 We will not reimburse word processing and meals.

6.2 Photocopying is reimbursed at no more than EUR 0.10 per page.

6.3 No more than two (2) timekeepers may bill for attending the same deposition without prior approval.
"""


def test_clauses_are_numbered_citable_and_keep_their_headings():
    doc = parse(GUIDE, "acme", 1, date(2026, 1, 1))
    ids = [c.id for c in doc.clauses]
    assert ids == ["0.p1", "3.1", "3.2", "5.1", "5.1(b)", "6.1", "6.2", "6.3"]
    assert "this bullet belongs to 3.2" in doc.clause("3.2").text  # type: ignore[union-attr]
    assert doc.clause("5.1").path == ("Section 5: Travel",)  # type: ignore[union-attr]


def test_pattern_extractor_finds_the_rules_with_quotes_that_verify():
    doc = parse(GUIDE, "acme", 1, date(2026, 1, 1))
    rules = {r["kind"]: r for r in extract(doc.clauses)}
    assert rules["billing_increment"]["hours"] == Decimal("0.1")
    assert rules["block_billing"]["reduce_factor"] == Decimal("0.5")
    assert rules["travel_time"]["rate_factor"] == Decimal("0.5")
    assert rules["nonreimbursable_expense"]["codes"] == ["E103", "E111"]
    assert rules["expense_unit_cap"]["max_unit_cost"] == Decimal("0.10")
    assert rules["max_attendees"]["max_timekeepers"] == 2
    for r in rules.values():
        assert r["quote"] in doc.clause(r["clause_id"]).text  # type: ignore[union-attr]


def test_verify_refuses_quotes_that_are_not_in_the_clause_or_do_not_state_the_value():
    text = "3.1 Time must be recorded in increments of one-tenth of an hour (0.1). Minimum charges are not accepted."
    ok = BillingIncrement(clause_id="3.1", quote="Time must be recorded in increments of one-tenth of an hour (0.1).", hours=Decimal("0.1"))
    assert verify(ok, text) == []
    wrong_value = BillingIncrement(
        clause_id="3.1", quote="Time must be recorded in increments of one-tenth of an hour (0.1).", hours=Decimal("0.25")
    )
    assert "0.25 is not stated in the quote" in verify(wrong_value, text)
    invented = BillingIncrement(clause_id="3.1", quote="Time must be recorded in quarter hours (0.25).", hours=Decimal("0.25"))
    assert verify(invented, text) == ["quote is not verbatim in the cited clause"]
    assert (
        verify(
            TravelTime(clause_id="5.1", quote="Travel time is not billable.", rate_factor=Decimal("0")), "5.1 Travel time is not billable."
        )
        == []
    )
    assert verify(
        TravelTime(clause_id="5.1", quote="Travel time is billable in full.", rate_factor=Decimal("0")),
        "5.1 Travel time is billable in full.",
    )
    cap = ExpenseUnitCap(
        clause_id="6.2", quote="Photocopying is reimbursed at no more than EUR 0.10 per page.", code="E101", max_unit_cost=Decimal("0.10")
    )
    assert verify(cap, "6.2 Photocopying is reimbursed at no more than EUR 0.10 per page.") == []
    nr = NonreimbursableExpenses(clause_id="6.1", quote="We will not reimburse word processing and meals.", codes=("E103", "E104"))
    assert any("E104" in p for p in verify(nr, "6.1 We will not reimburse word processing and meals."))
    att = MaxAttendees(
        clause_id="6.3",
        quote="No more than two (2) timekeepers may bill for attending the same deposition",
        event="hearing",
        max_timekeepers=2,
    )
    assert "hearing is not named in the quote" in verify(
        att, "6.3 No more than two (2) timekeepers may bill for attending the same deposition"
    )
