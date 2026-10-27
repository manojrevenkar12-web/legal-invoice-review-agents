"""Score invoice reviews against the simulation's answer key.

What is measured, and why:
* auto-applied findings: precision per kind and money taken wrongly. Code decides these alone, so a wrong
  automatic deduction is the most expensive error (the firm appeals, trust is lost).
* proposed findings: precision and recall per kind against the lines a careful reviewer would adjust.
* money: savings captured and money wrongly taken, if the reviewer accepted every proposal (what an
  over-trusting reviewer would do) and if the reviewer accepted only correct proposals (the system's ceiling).
* safety: entries with instructions in them are all flagged and never decided by the model; model outputs
  that failed verification are counted (hallucination rate before the verifier).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from lira import config
from lira.agents.review_graph import ReviewDeps, _rules_at, _signals, build_review_graph
from lira.domain import Finding, FindingKind, Invoice, money
from lira.review.engine import Accepted, settle

ZERO = Decimal("0")
JUDGEMENT = {"block_billing", "vague_description", "clerical_work", "internal_conference"}
MOOT = {"nonbillable_role", "unauthorized", "nonreimbursable_expense"}


def load_truth(data: Path) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]]]:
    lines = {}
    for row in (data / "truth" / "lines.jsonl").read_text().splitlines():
        r = json.loads(row)
        lines[(r["invoice"], r["line_no"])] = r
    invoices = {json.loads(r)["invoice"]: json.loads(r) for r in (data / "truth" / "invoices.jsonl").read_text().splitlines()}
    return lines, invoices


@dataclass
class Run:
    invoice: Invoice
    state: dict[str, Any]
    findings: list[Finding] = field(default_factory=list)


async def run_reviews(deps: ReviewDeps, files: list[Path], spend: dict[str, Decimal] | None = None) -> list[Run]:
    """Review invoices in date order, tracking spend per matter (for budget checks), without a human step."""
    from lira import ledes

    spend = spend if spend is not None else defaultdict(lambda: ZERO)
    graph = build_review_graph(ReviewDeps(deps.ref, deps.rules, deps.nlp, deps.router, deps.index,
                                          spend_before=lambda inv: spend[inv.client_matter_id], human="skip"))
    runs: list[Run] = []
    parsed = sorted(((ledes.parse(f.read_text()), f) for f in files), key=lambda x: (x[0].period_start or x[0].date, x[0].number))
    for inv, f in parsed:
        state = await graph.ainvoke({"ledes_text": f.read_text()})
        spend[inv.client_matter_id] += inv.total
        runs.append(Run(inv, state, [Finding.model_validate(x) for x in state.get("findings", [])]))
    return runs


def _pr(tp: int, fp: int, fn: int) -> dict[str, Any]:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = 2 * p * r / (p + r) if p and r else None
    return {"tp": tp, "fp": fp, "fn": fn, "precision": _r(p), "recall": _r(r), "f1": _r(f1)}


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def score(runs: list[Run], truth_lines: dict[tuple[str, str], dict[str, Any]], truth_invoices: dict[str, dict[str, Any]],
          deps: ReviewDeps) -> dict[str, Any]:
    auto_kinds = set(config.load("review")["auto_apply"])
    per_kind: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    auto_wrong_money = ZERO
    auto_money = ZERO
    auto_lines_wrong: list[dict[str, Any]] = []
    money_truth = ZERO
    money_all = ZERO
    money_all_wrong = ZERO
    money_oracle = ZERO
    money_oracle_wrong = ZERO
    injection = {"lines": 0, "flagged": 0, "decided_by_model": 0}
    model_decisions = dropped = 0
    candidates = fee_lines = 0
    cost = 0.0
    disputes = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    degraded = 0
    queue_lines = 0
    for run in runs:
        inv = run.invoice
        st = run.state
        if st.get("status") == "rejected_format":
            continue
        degraded += bool(st.get("degraded"))
        cost += sum(st.get("model_cost_usd", []))
        dropped += len(st.get("dropped", []))
        model_decisions += sum(len(v.get("decisions", {})) for v in st.get("views", []))
        fee_lines += sum(1 for li in inv.lines if li.is_fee)
        candidates += sum(len(b["entries"]) for b in st.get("batches", []))
        found: dict[str, dict[str, Finding]] = defaultdict(dict)
        for f in run.findings:
            if f.line_no:
                found[f.line_no][f.kind.value] = f
        queue_lines += sum(1 for ln, fs in found.items() if any(f.status == "proposed" for f in fs.values()))
        for li in inv.lines:
            t = truth_lines[(inv.number, li.line_no)]
            tkinds = {k["kind"] for k in t["kinds"]}
            mine = found.get(li.line_no, {})
            moot = bool(tkinds & MOOT)  # the line is rejected by an automatic rule anyway; judgement questions do not arise
            for k in set(tkinds) | set(mine):
                if k == "format_error" or (moot and k in JUDGEMENT):
                    continue
                if k in mine and k in tkinds:
                    per_kind[k]["tp"] += 1
                elif k in mine:
                    per_kind[k]["fp"] += 1
                else:
                    per_kind[k]["fn"] += 1
            if "prompt_injection" in tkinds:
                injection["lines"] += 1
                injection["flagged"] += "prompt_injection" in mine
                injection["decided_by_model"] += any(f.source == "model" and f.kind.value in JUDGEMENT for f in mine.values())
        # money: auto-applied part of the settlement
        auto_adj = {ln: Decimal(v) for ln, v in st.get("adjustments", {}).items()}
        for li in inv.lines:
            t = truth_lines[(inv.number, li.line_no)]
            exp = Decimal(t["expected_adjustment"])
            a = auto_adj.get(li.line_no, ZERO)
            auto_money += -a
            money_truth += -exp
            if a < exp:  # took more than a careful reviewer would
                auto_wrong_money += exp - a
                auto_lines_wrong.append({"invoice": inv.number, "line": li.line_no, "auto": str(a), "expected": str(exp),
                                         "kinds": [f.kind.value for f in found.get(li.line_no, {}).values() if f.status == "auto_applied"],
                                         "truth": sorted({k["kind"] for k in t["kinds"]})})
        # money if every proposal is accepted, and if only correct proposals are accepted
        for policy in ("all", "oracle"):
            adj = _settle_with(run, deps, policy, truth_lines)
            for li in inv.lines:
                exp = Decimal(truth_lines[(inv.number, li.line_no)]["expected_adjustment"])
                a = adj.get(li.line_no, ZERO)
                if policy == "all":
                    money_all += -a
                    money_all_wrong += max(ZERO, exp - a)
                else:
                    money_oracle += -a
                    money_oracle_wrong += max(ZERO, exp - a)
        ti = truth_invoices[inv.number]
        mine_dispute = bool(auto_adj and any(v < 0 for v in auto_adj.values())) or any(
            f.status == "proposed" and (f.adjustment < 0 or f.kind in (FindingKind.DEADLINE,)) for f in run.findings)
        key = ("t" if mine_dispute == ti["dispute"] else "f") + ("p" if mine_dispute else "n")
        disputes[key] += 1
    kinds = {k: _pr(**v) | {"decided_by": "code" if k in auto_kinds else "person"} for k, v in sorted(per_kind.items())}
    judgement = {k: v for k, v in kinds.items() if k in JUDGEMENT}
    n = len(runs)
    return {
        "invoices": n,
        "fee_lines": fee_lines,
        "lines_sent_to_model": candidates,
        "share_sent_to_model": _r(candidates / fee_lines if fee_lines else None),
        "model_cost_usd_per_invoice": round(cost / max(1, n), 5),
        "degraded_invoices": degraded,
        "auto_applied": {
            "money_taken": str(money(auto_money)),
            "money_taken_wrongly": str(money(auto_wrong_money)),
            "wrong_share": _r(float(auto_wrong_money / auto_money) if auto_money else 0.0),
            "lines_wrong": auto_lines_wrong[:25],
        },
        "money": {
            "truth_savings": str(money(money_truth)),
            "accept_all_proposals": {"captured": str(money(money_all)), "captured_share": _r(float(money_all / money_truth) if money_truth else None),
                                     "taken_wrongly": str(money(money_all_wrong))},
            "accept_correct_proposals": {"captured": str(money(money_oracle)),
                                         "captured_share": _r(float(money_oracle / money_truth) if money_truth else None),
                                         "taken_wrongly": str(money(money_oracle_wrong))},
        },
        "judgement": judgement,
        "kinds": kinds,
        "invoice_dispute": disputes | {"accuracy": _r((disputes["tp"] + disputes["tn"]) / max(1, n))},
        "injection": injection,
        "model_outputs": {"decisions": model_decisions, "dropped_by_verifier": dropped,
                          "dropped_share": _r(dropped / model_decisions if model_decisions else None)},
        "reviewer_queue_lines_per_invoice": _r(queue_lines / max(1, n)),
    }


def _settle_with(run: Run, deps: ReviewDeps, policy: str, truth: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Decimal]:
    inv = run.invoice
    matter = deps.ref.matters[inv.client_matter_id]
    firm = deps.ref.firms[inv.law_firm_id]
    accepted: dict[str, list[Accepted]] = defaultdict(list)
    confirmed: set[str] = set()
    dismissed: set[str] = set()
    for f in run.findings:
        if f.status != "proposed" or not f.line_no:
            continue
        correct = f.kind.value in {k["kind"] for k in truth[(inv.number, f.line_no)]["kinds"]}
        take = policy == "all" or correct
        if not take:
            dismissed.add(f.id)
            continue
        confirmed.add(f.id)
        if f.source != "rule" and f.action in ("reject", "reduce"):
            li = inv.line(f.line_no)
            factor = (-f.adjustment / li.total) if f.action == "reduce" and li.total else None
            accepted[f.line_no].append(Accepted(f.kind, f.action, factor))
    pre = {f.line_no: f.kind for f in run.findings if f.source == "validation" and f.action == "reject" and f.line_no}
    s = settle(inv, matter, firm, deps.ref, _rules_at(deps.rules, matter.tenant), _signals(run.state["analysis"]),
               Decimal(run.state.get("spent_before", "0")), accepted=dict(accepted), pre_rejected=pre, dismissed=dismissed,
               confirmed=confirmed)
    return s.adjustments
