"""The answer key: what a careful reviewer applying the client's real guideline parameters would do to each line.

This is a second, independent implementation of the adjustment arithmetic, written from the parameters in
config/world.yaml rather than from compiled rules, so the evaluation does not grade the engine against
itself. The arithmetic (docs/ADJUSTMENTS.md):

* a line is rejected (adjusted to zero) if any rejecting rule applies;
* otherwise hours are floored to the billing increment, research above the monthly cap and hours above the
  daily cap are removed (latest lines first), the rate is capped at the approved rate (times the travel
  factor for travel), and block-billed entries are reduced by the stated share;
* expenses that are not reimbursable are rejected, copying above the per-page cap is reduced to the cap.

Simulation only.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from lira.domain import Firm, Invoice, Matter, money

TENTH = Decimal("0.1")


def floor_tenth(x: Decimal) -> Decimal:
    return (x / TENTH).to_integral_value(rounding=ROUND_FLOOR) * TENTH


def score_invoice(
    inv: Invoice,
    m: Matter,
    firm: Firm,
    facts: dict[str, dict[str, Any]],
    params_at: Callable[[str, date], dict[str, Any]],
    approved_rate: Callable[[str, str, date], Decimal | None],
    spent: dict[str, Decimal],
    month_index: int,
    history_months: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    kinds: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rejected: set[str] = set()

    def hit(ln: str, kind: str, action: str) -> None:
        kinds[ln].append({"kind": kind, "action": action})
        if action == "reject":
            rejected.add(ln)

    fees = [li for li in inv.lines if li.is_fee]
    # --- rejecting rules and flags, line by line
    for li in inv.lines:
        f = facts[li.line_no]["facts"]
        p = params_at(m.tenant, li.date)
        if li.is_expense:
            if li.expense_code in (p.get("nonreimbursable") or []):
                hit(li.line_no, "nonreimbursable_expense", "reject")
            cap = (p.get("expense_caps") or {}).get(li.expense_code)
            if cap is not None and li.unit_cost > Decimal(str(cap)):
                hit(li.line_no, "expense_unit_cap", "reduce")
            continue
        tk = firm.timekeeper(li.timekeeper_id)
        title = tk.title.lower() if tk else ""
        if f.get("unauthorized"):
            hit(li.line_no, "unauthorized", "reject")
        if any(role in title for role in (p.get("nonbillable_roles") or [])):
            hit(li.line_no, "nonbillable_role", "reject")
        if f.get("vague") and p.get("vague"):
            hit(li.line_no, "vague_description", "reject")
        if f.get("clerical") and p.get("clerical"):
            hit(li.line_no, "clerical_work", "reject")
        if f.get("block"):
            act = p["block_billing"]["action"]
            hit(li.line_no, "block_billing", "reject" if act == "reject" else "reduce")
        if f.get("travel") and p.get("travel_factor") is not None:
            factor = Decimal(str(p["travel_factor"]))
            billed_factor = facts[li.line_no].get("rate_factor", Decimal("1"))
            if factor == 0:
                hit(li.line_no, "travel_time", "reject")
            elif billed_factor > factor:
                hit(li.line_no, "travel_time", "reduce")
        if f.get("increment_slip") and not ({k["kind"] for k in kinds.get(li.line_no, [])} & {"nonbillable_role", "unauthorized"}):
            hit(li.line_no, "billing_increment", "reduce")
        if f.get("true_task"):
            hit(li.line_no, "task_code_mismatch", "flag")
        if f.get("inflated"):
            hit(li.line_no, "unusual_time", "flag")
        if f.get("injection"):
            hit(li.line_no, "prompt_injection", "flag")

    # --- internal conferences: both sides of a meeting are in this invoice
    meetings: dict[str, list[Any]] = defaultdict(list)
    for li in fees:
        mid = facts[li.line_no]["facts"].get("internal_conference")
        if mid:
            meetings[mid].append(li)
    for lis in meetings.values():
        p = params_at(m.tenant, lis[0].date)
        policy = p.get("internal_conference")
        if policy == "not_billable":
            for li in lis:
                hit(li.line_no, "internal_conference", "reject")
        elif policy == "one_attendee" and len(lis) > 1:
            keep = _most_senior(lis, firm)
            for li in lis:
                if li.line_no != keep:
                    hit(li.line_no, "internal_conference", "reject")

    # --- attendance caps for depositions and hearings
    events: dict[str, list[Any]] = defaultdict(list)
    for li in fees:
        ev = facts[li.line_no]["facts"].get("event")
        if ev:
            events[ev].append(li)
    for lis in events.values():
        p = params_at(m.tenant, lis[0].date)
        kind = facts[lis[0].line_no]["facts"]["event_kind"]
        cap = (p.get("max_attendees") or {}).get(kind)
        if cap is None or len(lis) <= cap:
            continue
        ordered = sorted(lis, key=lambda li: (-_seniority(li, firm), int(li.line_no)))
        for li in ordered[cap:]:
            hit(li.line_no, "max_attendees", "reject")

    # --- hours: increment floor, research cap, daily cap (rejected lines do not count)
    units = {li.line_no: (floor_tenth(li.units) if li.units % TENTH else li.units) for li in fees}
    research_used: dict[tuple[str, int, int], Decimal] = defaultdict(lambda: Decimal("0"))
    for li in sorted(fees, key=lambda x: (x.date, int(x.line_no))):
        if li.line_no in rejected or li.activity_code != "A102":
            continue
        cap = params_at(m.tenant, li.date).get("research_cap")
        if cap is None:
            continue
        key = (m.id, li.date.year, li.date.month)
        allowed = max(Decimal("0"), Decimal(str(cap)) - research_used[key])
        research_used[key] += units[li.line_no]
        if units[li.line_no] > allowed:
            units[li.line_no] = allowed
            hit(li.line_no, "research_cap", "reduce")
    by_day: dict[tuple[str, date], list[Any]] = defaultdict(list)
    for li in fees:
        if li.line_no not in rejected:
            by_day[(li.timekeeper_id, li.date)].append(li)
    for (_tk, d), lis in by_day.items():
        cap = params_at(m.tenant, d).get("max_daily_hours")
        if cap is None:
            continue
        total = sum((units[li.line_no] for li in lis), Decimal("0"))
        excess = total - Decimal(str(cap))
        for li in sorted(lis, key=lambda x: -int(x.line_no)):
            if excess <= 0:
                break
            cut = min(excess, units[li.line_no])
            units[li.line_no] -= cut
            excess -= cut
            hit(li.line_no, "max_daily_hours", "reduce")

    # --- amounts
    out: list[dict[str, Any]] = []
    for li in inv.lines:
        f = facts[li.line_no]["facts"]
        p = params_at(m.tenant, li.date)
        outright = {k["kind"] for k in kinds.get(li.line_no, [])} & {"nonbillable_role", "unauthorized"}
        if li.is_fee and not f.get("travel") and not outright:
            ok_rate = approved_rate(m.tenant, li.timekeeper_id, li.date)
            if ok_rate is not None and li.unit_cost > ok_rate and not any(k["kind"] == "rate_cap" for k in kinds.get(li.line_no, [])):
                hit(li.line_no, "rate_cap", "reduce")
        if li.line_no in rejected:
            expected = -li.total
        elif li.is_expense:
            cap = (p.get("expense_caps") or {}).get(li.expense_code)
            expected = money(min(li.unit_cost, Decimal(str(cap))) * li.units) - li.total if cap is not None else Decimal("0")
        else:
            ok_rate = approved_rate(m.tenant, li.timekeeper_id, li.date)
            allowed = ok_rate if ok_rate is not None else li.unit_cost
            if f.get("travel") and p.get("travel_factor") is not None:
                allowed = money(allowed * Decimal(str(p["travel_factor"])))
                if li.unit_cost > allowed and not any(k["kind"] == "travel_time" for k in kinds.get(li.line_no, [])):
                    hit(li.line_no, "travel_time", "reduce")
            allowed = min(allowed, li.unit_cost)
            amount = units[li.line_no] * allowed
            if f.get("block") and p["block_billing"]["action"] == "reduce":
                amount = amount * (1 - Decimal(str(p["block_billing"]["factor"])))
            expected = money(amount) - li.total
        out.append({
            "invoice": inv.number, "tenant": m.tenant, "firm": m.firm_id, "matter": m.id, "line_no": li.line_no,
            "date": li.date, "type": li.type.value, "total": li.total, "kinds": kinds.get(li.line_no, []),
            "expected_adjustment": money(min(Decimal("0"), expected)),
            "true_task_code": f.get("true_task") or (li.task_code if li.is_fee else ""),
            "facts": {k: v for k, v in f.items() if k in ("vague", "clerical", "block", "tasks", "travel", "halved", "research",
                                                           "internal_conference", "event", "event_kind", "injection", "inflated",
                                                           "unauthorized", "increment_slip")},
            "split": "history" if month_index < history_months else "live",
        })

    # --- invoice level
    p_end = params_at(m.tenant, inv.period_end or inv.date)
    late = inv.period_end is not None and (inv.date - inv.period_end).days > int(p_end["submission_days"])
    before = spent[m.id]
    spent[m.id] = before + inv.total
    over_budget = spent[m.id] > m.budget
    inv_truth = {"invoice": inv.number, "tenant": m.tenant, "firm": m.firm_id, "matter": m.id, "late": late,
                 "over_budget": over_budget, "total": inv.total,
                 "expected_adjustment": money(sum((r["expected_adjustment"] for r in out), Decimal("0"))),
                 "split": "history" if month_index < history_months else "live",
                 "dispute": bool(late or any(r["expected_adjustment"] < 0 for r in out))}
    return out, inv_truth


def _seniority(li: Any, firm: Firm) -> int:
    tk = firm.timekeeper(li.timekeeper_id)
    return tk.seniority if tk else -1


def _most_senior(lis: list[Any], firm: Firm) -> str:
    return sorted(lis, key=lambda li: (-_seniority(li, firm), int(li.line_no)))[0].line_no
