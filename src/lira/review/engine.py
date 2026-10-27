"""Apply the approved rules to an invoice and settle every line's amount.

Settlement arithmetic (also in docs/ADJUSTMENTS.md):
1. A line is rejected (paid 0) if any rejecting finding applies: nonbillable role, non-reimbursable expense,
   unauthorised timekeeper, non-billable travel, attendee above the cap, or an accepted judgement rejection.
2. Otherwise hours are floored to the billing increment; research hours above the monthly cap and hours above
   the daily cap are removed, latest lines first (rejected lines do not count towards either cap).
3. The rate is capped at the approved rate, times the travel factor for travel; copying at the per-page cap.
4. An accepted block-billing reduction takes its share off what remains.
Each finding records the amount its own step removed; the line's adjustment is the settled amount minus the total.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from lira import config
from lira.domain import ClauseRef, Evidence, Finding, FindingKind, Firm, Invoice, LineItem, Matter, money
from lira.guidelines.rules import (
    BillingIncrement,
    ExpenseUnitCap,
    MaxAttendees,
    MaxDailyHours,
    NonbillableRoles,
    NonreimbursableExpenses,
    ResearchCap,
    RuleSet,
    SubmissionDeadline,
    TravelTime,
)
from lira.reference import Reference

ZERO = Decimal("0")


@dataclass
class LineSignals:
    """What reading the line established (classical NLP); the engine only consumes these flags."""

    travel: bool = False
    event_kind: str | None = None  # "deposition" | "hearing"
    event_key: str | None = None  # same key = same event attended by several timekeepers


@dataclass
class Accepted:
    """A judgement finding a person accepted (or that the evaluation assumes accepted)."""

    kind: FindingKind
    action: str  # "reject" | "reduce"
    reduce_factor: Decimal | None = None


@dataclass
class Settlement:
    findings: list[Finding] = field(default_factory=list)
    adjustments: dict[str, Decimal] = field(default_factory=dict)  # line_no -> settled minus billed (<= 0)
    rejected: dict[str, FindingKind] = field(default_factory=dict)

    @property
    def total_adjustment(self) -> Decimal:
        return money(sum(self.adjustments.values(), ZERO))


def _ref(rs: RuleSet, rule: Any) -> ClauseRef:
    return ClauseRef(document=rs.document, clause_id=rule.clause_id, quote=rule.quote)


def _floor(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_FLOOR) * step


def settle(inv: Invoice, matter: Matter, firm: Firm, ref: Reference, rules_at: Callable[[date], RuleSet | None],
           signals: dict[str, LineSignals], spent_before: Decimal = ZERO, accepted: dict[str, list[Accepted]] | None = None,
           pre_rejected: dict[str, FindingKind] | None = None, dismissed: set[str] | None = None,
           confirmed: set[str] | None = None, uncertain: set[str] | None = None) -> Settlement:
    cfg = config.load("review")
    auto = set(cfg["auto_apply"])
    accepted = accepted or {}
    dismissed = dismissed or set()
    confirmed = confirmed or set()
    uncertain = uncertain or set()  # lines with an open question (a proposal nobody has decided yet)
    out = Settlement()
    tenant = matter.tenant

    def finding(li: LineItem | None, kind: FindingKind, action: str, rs: RuleSet | None, rule: Any, rationale: str,
                evidence: list[Evidence] | None = None, delta: Decimal = ZERO, pending: bool = False) -> Finding:
        fid = fid_of(li, kind)
        decided_by = "person" if pending or kind.value not in auto else "rule"
        status = "rejected" if fid in dismissed else "proposed" if decided_by == "person" else "auto_applied"
        f = Finding(
            id=fid, invoice=inv.number, line_no=li.line_no if li else None,
            kind=kind, source="rule", decided_by=decided_by, action=action,  # type: ignore[arg-type]
            adjustment=money(delta), clause=_ref(rs, rule) if rs and rule else None, evidence=evidence or [], confidence=1.0,
            rationale=rationale, status=status,  # type: ignore[arg-type]
        )
        if fid in dismissed:
            f.adjustment = ZERO
        out.findings.append(f)
        return f

    def fid_of(li: LineItem | None, kind: FindingKind) -> str:
        return f"{inv.number}:{li.line_no if li else ''}:{kind.value}"

    def live(li: LineItem | None, kind: FindingKind) -> bool:
        """False when a person dismissed this finding: it is recorded, but has no effect on the amount."""
        return fid_of(li, kind) not in dismissed

    def effective(li: LineItem | None, kind: FindingKind) -> bool:
        """Changes the amount now: auto-applied kinds, or a proposed finding a person confirmed."""
        return live(li, kind) and (kind.value in auto or fid_of(li, kind) in confirmed)

    rules = {li.line_no: rules_at(li.date) for li in inv.lines}
    rejected: dict[str, FindingKind] = dict(pre_rejected or {})

    # ---- 1. rejecting rules
    for li in inv.lines:
        rs = rules[li.line_no]
        if rs is None:
            continue
        if li.is_expense:
            r = rs.one("nonreimbursable_expense")
            if isinstance(r, NonreimbursableExpenses) and li.expense_code in r.codes:
                finding(li, FindingKind.EXPENSE, "reject", rs, r, f"Expense code {li.expense_code} is not reimbursed under clause {r.clause_id}.",
                        [Evidence(source="line", text=li.expense_code)], -li.total)
                if effective(li, FindingKind.EXPENSE):
                    rejected.setdefault(li.line_no, FindingKind.EXPENSE)
            continue
        tk = firm.timekeeper(li.timekeeper_id)
        r = rs.one("nonbillable_role")
        if tk and isinstance(r, NonbillableRoles):
            title = tk.title.lower()
            role = next((x for x in r.roles if x.lower() in title or (x.lower() == "trainee" and "trainee" in title)), None)
            if role:
                finding(li, FindingKind.ROLE, "reject", rs, r, f"{tk.title} time is not billable under clause {r.clause_id}.",
                        [Evidence(source="rule", text=f"timekeeper {tk.id}: {tk.title}")], -li.total)
                if effective(li, FindingKind.ROLE):
                    rejected.setdefault(li.line_no, FindingKind.ROLE)
        sig = signals.get(li.line_no, LineSignals())
        r = rs.one("travel_time")
        if sig.travel and isinstance(r, TravelTime) and r.rate_factor == 0:
            finding(li, FindingKind.TRAVEL, "reject", rs, r, f"Travel time is not billable under clause {r.clause_id}.",
                    [Evidence(source="nlp", text="entry describes travel")], -li.total)
            if effective(li, FindingKind.TRAVEL):
                rejected.setdefault(li.line_no, FindingKind.TRAVEL)

    # attendance caps: one event, several timekeepers
    groups: dict[str, list[LineItem]] = defaultdict(list)
    for li in inv.lines:
        sig = signals.get(li.line_no)
        if li.is_fee and sig and sig.event_key:
            groups[sig.event_key].append(li)
    for key, lis in groups.items():
        rs = rules[lis[0].line_no]
        kind = signals[lis[0].line_no].event_kind
        cap = next((r for r in (rs.of("max_attendees") if rs else []) if isinstance(r, MaxAttendees) and r.event == kind), None)
        if cap is None or len(lis) <= cap.max_timekeepers:
            continue
        ordered = sorted(lis, key=lambda x: (-_seniority(firm, x), int(x.line_no) if x.line_no.isdigit() else 0))
        for li in ordered[cap.max_timekeepers:]:
            finding(li, FindingKind.ATTENDEES, "reject", rs, cap,
                    f"{len(lis)} timekeepers billed the same {kind}; clause {cap.clause_id} pays {cap.max_timekeepers}.",
                    [Evidence(source="cross_line", text=f"same {kind}: lines {', '.join(x.line_no for x in ordered)}")], -li.total)
            if effective(li, FindingKind.ATTENDEES):
                rejected.setdefault(li.line_no, FindingKind.ATTENDEES)

    for ln, decisions in accepted.items():
        if any(d.action == "reject" for d in decisions):
            rejected.setdefault(ln, next(d.kind for d in decisions if d.action == "reject"))

    # ---- 2. hours
    fees = [li for li in inv.lines if li.is_fee]
    units: dict[str, Decimal] = {li.line_no: li.units for li in fees}
    steps: dict[str, dict[str, Decimal]] = defaultdict(dict)  # line -> step -> units after the step
    for li in fees:
        rs = rules[li.line_no]
        r = rs.one("billing_increment") if rs else None
        if isinstance(r, BillingIncrement) and li.units % r.hours != 0 and live(li, FindingKind.INCREMENT):
            units[li.line_no] = _floor(li.units, r.hours)
            steps[li.line_no]["increment"] = units[li.line_no]
    research_used: dict[tuple[int, int], Decimal] = defaultdict(lambda: ZERO)
    research_rule: dict[str, Any] = {}
    open_months = {(li.date.year, li.date.month) for li in fees
                   if li.line_no in uncertain and li.activity_code == "A102" and li.line_no not in rejected}
    pending_units: dict[str, Decimal] = {}  # cuts that wait for a decision elsewhere: shown, not applied
    for li in sorted(fees, key=lambda x: (x.date, _num(x.line_no))):
        if li.line_no in rejected or li.activity_code != "A102":
            continue
        rs = rules[li.line_no]
        r = rs.one("research_cap") if rs else None
        if not isinstance(r, ResearchCap) or not live(li, FindingKind.RESEARCH):
            continue
        k = (li.date.year, li.date.month)
        allowed = max(ZERO, r.hours_per_month - research_used[k])
        research_used[k] += units[li.line_no]
        if units[li.line_no] > allowed:
            research_rule[li.line_no] = (rs, r)
            if k in open_months and fid_of(li, FindingKind.RESEARCH) not in confirmed:
                steps[li.line_no]["research_pending"] = allowed
                continue
            units[li.line_no] = allowed
            steps[li.line_no]["research"] = allowed
    by_day: dict[tuple[str, date], list[LineItem]] = defaultdict(list)
    for li in fees:
        if li.line_no not in rejected:
            by_day[(li.timekeeper_id, li.date)].append(li)
    daily_rule: dict[str, Any] = {}
    for (_tk, d), lis in by_day.items():
        rs = rules_at(d)
        r = rs.one("max_daily_hours") if rs else None
        if not isinstance(r, MaxDailyHours):
            continue
        excess = sum((units[x.line_no] for x in lis), ZERO) - r.hours
        open_day = any(x.line_no in uncertain for x in lis)
        for li in sorted(lis, key=lambda x: -_num(x.line_no)):
            if excess <= 0:
                break
            if not live(li, FindingKind.DAILY_HOURS):
                continue
            cut = min(excess, units[li.line_no])
            if cut <= 0:
                continue
            excess -= cut
            daily_rule[li.line_no] = (rs, r, sum((x.units for x in lis), ZERO))
            if open_day and fid_of(li, FindingKind.DAILY_HOURS) not in confirmed:
                steps[li.line_no]["daily_pending"] = units[li.line_no] - cut
                continue
            units[li.line_no] -= cut
            steps[li.line_no]["daily"] = units[li.line_no]

    # ---- 3/4. amounts
    for li in inv.lines:
        rs = rules[li.line_no]
        if li.line_no in rejected:
            out.adjustments[li.line_no] = -li.total
            continue
        if li.is_expense:
            r = next((x for x in (rs.of("expense_unit_cap") if rs else []) if isinstance(x, ExpenseUnitCap) and x.code == li.expense_code), None)
            if r and li.unit_cost > r.max_unit_cost and live(li, FindingKind.EXPENSE_CAP):
                delta = money(r.max_unit_cost * li.units) - li.total
                finding(li, FindingKind.EXPENSE_CAP, "reduce", rs, r,
                        f"Billed {li.unit_cost} per unit; clause {r.clause_id} reimburses at most {r.max_unit_cost}.",
                        [Evidence(source="line", text=f"{li.units} x {li.unit_cost}")], delta)
                out.adjustments[li.line_no] = delta
            else:
                out.adjustments[li.line_no] = ZERO
            continue
        if not li.is_fee:
            out.adjustments[li.line_no] = ZERO
            continue
        amount_prev = li.total
        st = steps.get(li.line_no, {})
        if "increment" in st:
            a = money(st["increment"] * li.unit_cost)
            r = rs.one("billing_increment") if rs else None
            finding(li, FindingKind.INCREMENT, "reduce", rs, r, f"{li.units} h is not in {r.hours if r else ''} h increments; floored to {st['increment']} h.",
                    [Evidence(source="line", text=f"{li.units} h")], a - amount_prev)
            amount_prev = a
        if "research" in st:
            rs_r, r = research_rule[li.line_no]
            a = money(st["research"] * li.unit_cost)
            finding(li, FindingKind.RESEARCH, "reduce", rs_r, r, f"Research above {r.hours_per_month} h this month (clause {r.clause_id}).",
                    [Evidence(source="rule", text=f"activity {li.activity_code}")], a - amount_prev)
            amount_prev = a
        if "daily" in st:
            rs_d, r, day_total = daily_rule[li.line_no]
            a = money(st["daily"] * li.unit_cost)
            finding(li, FindingKind.DAILY_HOURS, "reduce", rs_d, r, f"{day_total} h billed on {li.date}; clause {r.clause_id} allows {r.hours} h.",
                    [Evidence(source="rule", text=f"{li.timekeeper_id} on {li.date}: {day_total} h")], a - amount_prev)
            amount_prev = a
        if "research_pending" in st:
            rs_r, r = research_rule[li.line_no]
            finding(li, FindingKind.RESEARCH, "reduce", rs_r, r,
                    f"Research above {r.hours_per_month} h this month (clause {r.clause_id}), if the open findings on this month's research stand.",
                    [Evidence(source="rule", text=f"activity {li.activity_code}")], money(st["research_pending"] * li.unit_cost) - amount_prev,
                    pending=True)
        if "daily_pending" in st:
            rs_d, r, day_total = daily_rule[li.line_no]
            finding(li, FindingKind.DAILY_HOURS, "reduce", rs_d, r,
                    f"{day_total} h billed on {li.date}; clause {r.clause_id} allows {r.hours} h, if the open findings on that day stand.",
                    [Evidence(source="rule", text=f"{li.timekeeper_id} on {li.date}: {day_total} h")],
                    money(st["daily_pending"] * li.unit_cost) - amount_prev, pending=True)
        approved = ref.approved_rate(tenant, li.timekeeper_id, li.date)
        allowed = approved if approved is not None else li.unit_cost
        sig = signals.get(li.line_no, LineSignals())
        travel = rs.one("travel_time") if rs else None
        kind = FindingKind.RATE
        if sig.travel and isinstance(travel, TravelTime) and live(li, FindingKind.TRAVEL):
            allowed = money(allowed * travel.rate_factor)
            kind = FindingKind.TRAVEL
        allowed = min(allowed, li.unit_cost)
        a = money(units[li.line_no] * allowed)
        if not effective(li, kind):  # recorded with the amount it would take, applied only once confirmed
            allowed_now = min(approved if approved is not None and live(li, FindingKind.RATE) else li.unit_cost, li.unit_cost)
        else:
            allowed_now = allowed
        if allowed < li.unit_cost:
            if kind == FindingKind.TRAVEL:
                finding(li, kind, "reduce", rs, travel, f"Travel billed at {li.unit_cost}; clause {travel.clause_id} pays {allowed} ({travel.rate_factor} of the approved rate).",  # type: ignore[union-attr]
                        [Evidence(source="nlp", text="entry describes travel")], a - amount_prev)
            else:
                r = rs.one("rate_cap") if rs else None
                finding(li, kind, "reduce", rs, r, f"Billed rate {li.unit_cost} is above the approved rate {approved} for {li.timekeeper_id} on {li.date}.",
                        [Evidence(source="rule", text=f"approved rate {approved}")], a - amount_prev)
        block = next((d for d in accepted.get(li.line_no, []) if d.kind == FindingKind.BLOCK and d.action == "reduce"), None)
        if block and block.reduce_factor:
            amount = money(units[li.line_no] * allowed_now * (1 - block.reduce_factor))
        else:
            amount = money(units[li.line_no] * allowed_now)
        out.adjustments[li.line_no] = min(ZERO, amount - li.total)

    # ---- invoice level
    rs_end = rules_at(inv.period_end or inv.date)
    r = rs_end.one("submission_deadline") if rs_end else None
    if isinstance(r, SubmissionDeadline) and inv.period_end and (inv.date - inv.period_end).days > r.days:
        finding(None, FindingKind.DEADLINE, "flag", rs_end, r,
                f"Invoice dated {(inv.date - inv.period_end).days} days after the billing period; clause {r.clause_id} allows {r.days}.",
                [Evidence(source="rule", text=f"period end {inv.period_end}, invoice date {inv.date}")])
    r = rs_end.one("budget") if rs_end else None
    if r is not None and spent_before + inv.total > matter.budget:
        finding(None, FindingKind.BUDGET, "flag", rs_end, r,
                f"Billed to date {money(spent_before + inv.total)} exceeds the approved budget {matter.budget}; check for an approved increase.",
                [Evidence(source="rule", text=f"budget {matter.budget}")])
    out.rejected = rejected
    return out


def _seniority(firm: Firm, li: LineItem) -> int:
    tk = firm.timekeeper(li.timekeeper_id)
    return tk.seniority if tk else -1


def _num(line_no: str) -> int:
    return int(line_no) if line_no.isdigit() else 0
