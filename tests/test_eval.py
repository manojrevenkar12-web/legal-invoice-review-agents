"""The evaluation machinery itself: the gate fails on regressions, agreement statistics, Bedrock export formats, scoring."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from lira.agents.review_graph import ReviewDeps
from lira.eval import bedrock_export, gate, judge, review_eval

from .conftest import live_invoices, router_with

REPORT = Path(__file__).resolve().parents[1] / "reports" / "eval" / "report.json"


@pytest.mark.skipif(not REPORT.exists(), reason="run `lira eval` first")
def test_the_committed_report_passes_the_gate_and_a_regression_fails_it():
    report = json.loads(REPORT.read_text())
    results = {name: ok for name, ok, _ in gate.check(report)}
    assert all(ok for name, ok in results.items() if not name.startswith("prompts")), [n for n, ok in results.items() if not ok]
    worse = copy.deepcopy(report)
    worse["golden"]["auto_applied"]["wrong_share"] = 0.01
    worse["faults"]["ungrounded_model_findings_shown"] = 1
    worse["retrieval"]["cross_tenant_hits"] = 1
    failed = {name for name, ok, _ in gate.check(worse) if not ok}
    assert {
        "review: money taken wrongly by automatic rules (share)",
        "faults: ungrounded model findings shown",
        "retrieval: cross-tenant hits",
    } <= failed


def test_kappa_and_agreement_against_adjudicated_labels():
    assert judge.kappa(["pass", "fail", "pass", "fail"], ["pass", "fail", "pass", "fail"]) == 1.0
    assert judge.kappa(["pass", "pass", "fail", "fail"], ["fail", "fail", "pass", "pass"]) == -1.0
    items = [{"id": str(i)} for i in range(4)]
    crit = {c: "pass" for c in judge.CRITERIA}
    a = {str(i): crit for i in range(4)}
    b = {"0": crit, "1": crit, "2": crit, "3": {**crit, "correct": "fail"}}
    j = {"0": crit, "1": crit, "2": {**crit, "correct": "fail"}, "3": crit}
    ag = judge.agreement(items, a, b, j)
    assert ag["correct"]["unsettled_items"] == 1 and ag["correct"]["settled_items"] == 3
    assert ag["correct"]["judge_vs_gold_agree"] == round(2 / 3, 4)


def test_bedrock_evaluation_records_follow_the_documented_formats():
    rag = bedrock_export.rag_records(
        [
            {
                "question": "Is travel billable?",
                "answer": "No [5.1]",
                "reference": "Travel time is not billable.",
                "passages": [{"clause_id": "5.1", "text": "Travel time is not billable."}],
            }
        ]
    )[0]
    turn = rag["conversationTurns"][0]
    assert turn["prompt"]["content"][0]["text"] == "Is travel billable?"
    assert turn["output"]["retrievedPassages"]["retrievalResults"][0]["content"]["text"] == "Travel time is not billable."
    rec = bedrock_export.finding_records(
        [
            {
                "clause_id": "3.2",
                "clause_text": "Block billing...",
                "line_no": "4",
                "timekeeper_role": "AS",
                "hours": "2.0",
                "description": "Draft; review",
                "kind": "block_billing",
                "action": "reduce",
                "explanation": "Line 4 combines several tasks.",
            }
        ]
    )[0]
    assert set(rec) == {"prompt", "category", "modelResponses"} and len(rec["modelResponses"]) == 1
    assert "{{prediction}}" in bedrock_export.GROUNDED_METRIC["customMetricDefinition"]["instructions"]


async def test_scoring_counts_findings_and_money_against_the_answer_key(ctx, built):
    deps = ReviewDeps(ctx.ref, ctx.rules, ctx.nlp, router_with({}), ctx.index)
    runs = await review_eval.run_reviews(deps, live_invoices(built["data"])[:8])
    tl, ti = review_eval.load_truth(built["data"])
    s = review_eval.score(runs, tl, ti, deps)
    assert s["invoices"] == 8
    assert s["auto_applied"]["money_taken_wrongly"] == "0.00"
    assert s["money"]["accept_correct_proposals"]["taken_wrongly"] == "0.00"
    assert 0 < s["share_sent_to_model"] < 0.5
