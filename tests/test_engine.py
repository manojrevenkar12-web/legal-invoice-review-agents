"""The settlement arithmetic. The strongest check: given the facts a perfect reader would establish (travel, events,
accepted judgement findings), the engine reproduces the answer key's expected adjustment on every line of the year,
although the answer key is computed by an independent implementation from the raw guideline parameters."""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from lira import ledes
from lira.domain import FindingKind
from lira.guidelines.store import RuleStore
from lira.reference import Reference
from lira.review.engine import Accepted, LineSignals, settle

JUDGEMENT = {
    "block_billing": FindingKind.BLOCK,
    "vague_description": FindingKind.VAGUE,
    "clerical_work": FindingKind.CLERICAL,
    "internal_conference": FindingKind.CONFERENCE,
}


def _truth(data: Path) -> dict[tuple[str, str], dict]:
    return {(r["invoice"], r["line_no"]): r for r in map(json.loads, (data / "truth" / "lines.jsonl").read_text().splitlines())}


def test_engine_reproduces_the_answer_key_given_perfect_reading(built):
    data = built["data"]
    ref = Reference.load(data / "world")
    rules = RuleStore(data / "rulesets")
    truth = _truth(data)
    spent: dict[str, Decimal] = defaultdict(Decimal)
    mismatches, duplicates = [], []
    files = sorted((data / "world" / "invoices").glob("*/*/*.ledes"), key=lambda p: (p.parent.name, p.name))
    for f in files:
        inv = ledes.parse(f.read_text())
        matter = ref.matters[inv.client_matter_id]
        signals: dict[str, LineSignals] = {}
        accepted: dict[str, list[Accepted]] = {}
        confirmed: set[str] = set()
        pre: dict[str, FindingKind] = {}
        for li in inv.lines:
            t = truth[(inv.number, li.line_no)]
            facts = t["facts"]
            signals[li.line_no] = LineSignals(
                travel=bool(facts.get("travel")), event_kind=facts.get("event_kind"), event_key=facts.get("event")
            )
            for k in t["kinds"]:
                if k["kind"] in JUDGEMENT:
                    rs = rules.in_force(matter.tenant, li.date)
                    rule = rs.one(k["kind"])
                    factor = getattr(rule, "reduce_factor", None) if k["action"] == "reduce" else None
                    accepted.setdefault(li.line_no, []).append(Accepted(JUDGEMENT[k["kind"]], k["action"], factor))
                if k["kind"] == "unauthorized":
                    pre[li.line_no] = FindingKind.UNAUTHORIZED
                confirmed.add(f"{inv.number}:{li.line_no}:{k['kind']}")
        s = settle(
            inv,
            matter,
            ref.firms[inv.law_firm_id],
            ref,
            lambda d, t=matter.tenant: rules.in_force(t, d),
            signals,
            spent[matter.id],
            accepted=accepted,
            pre_rejected=pre,
            confirmed=confirmed,
        )
        open_ = settle(
            inv, matter, ref.firms[inv.law_firm_id], ref, lambda d, t=matter.tenant: rules.in_force(t, d), signals, spent[matter.id]
        )
        for result in (s, open_):  # decided and still open: one finding per line and kind either way
            ids = [x.id for x in result.findings]
            duplicates.extend(i for i in set(ids) if ids.count(i) > 1)
        spent[matter.id] += inv.total
        for li in inv.lines:
            want = Decimal(truth[(inv.number, li.line_no)]["expected_adjustment"])
            got = s.adjustments.get(li.line_no, Decimal("0"))
            if got != want:
                mismatches.append((inv.number, li.line_no, str(want), str(got)))
    assert mismatches == []
    assert duplicates == []
    assert len(files) > 200


def test_a_dismissed_finding_no_longer_changes_the_amount(built, ctx):
    data = built["data"]
    truth = _truth(data)
    f = next(
        p
        for p in sorted((data / "world" / "invoices").glob("*/*/*.ledes"))
        if any(
            truth[(ledes.parse(p.read_text()).number, li.line_no)]["kinds"]
            and any(k["kind"] == "nonbillable_role" for k in truth[(ledes.parse(p.read_text()).number, li.line_no)]["kinds"])
            for li in ledes.parse(p.read_text()).lines
        )
    )
    inv = ledes.parse(f.read_text())
    m = ctx.ref.matters[inv.client_matter_id]
    at = lambda d: ctx.rules.in_force(m.tenant, d)  # noqa: E731
    first = settle(inv, m, ctx.ref.firms[inv.law_firm_id], ctx.ref, at, {})
    role = next(x for x in first.findings if x.kind == FindingKind.ROLE)
    assert role.status == "auto_applied" and first.adjustments[role.line_no] == -inv.line(role.line_no).total  # type: ignore[arg-type]
    again = settle(inv, m, ctx.ref.firms[inv.law_firm_id], ctx.ref, at, {}, dismissed={role.id})
    assert again.adjustments[role.line_no] == 0  # type: ignore[index]
    assert next(x for x in again.findings if x.id == role.id).status == "rejected"


def test_hour_caps_wait_while_a_related_question_is_open(ctx, built):
    """A daily cap depends on which lines of that day survive; while one is undecided the cut is proposed, not applied."""
    data = built["data"]
    truth = _truth(data)
    for f in sorted((data / "world" / "invoices").glob("kestrel/*/*.ledes")):
        inv = ledes.parse(f.read_text())
        if not any(any(k["kind"] == "max_daily_hours" for k in truth[(inv.number, li.line_no)]["kinds"]) for li in inv.lines):
            continue
        m = ctx.ref.matters[inv.client_matter_id]
        at = lambda d, t=m.tenant: ctx.rules.in_force(t, d)  # noqa: E731
        firm = ctx.ref.firms[inv.law_firm_id]
        sure = settle(inv, m, firm, ctx.ref, at, {})
        daily = [x for x in sure.findings if x.kind == FindingKind.DAILY_HOURS]
        if not daily:
            continue
        line = daily[0].line_no
        same_day = {li.line_no for li in inv.lines if li.date == inv.line(line).date and li.timekeeper_id == inv.line(line).timekeeper_id}  # type: ignore[arg-type]
        held = settle(inv, m, firm, ctx.ref, at, {}, uncertain=same_day)
        d2 = next(x for x in held.findings if x.id == daily[0].id)
        assert d2.status == "proposed" and d2.decided_by == "person"
        assert held.adjustments[line] == 0 or held.adjustments[line] > sure.adjustments[line]  # type: ignore[index]
        return
    raise AssertionError("no invoice with a daily-hours cut found")
