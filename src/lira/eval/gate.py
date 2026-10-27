"""The release gate: compare reports/eval/report.json with config/release.yaml. Any miss fails the build."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lira import config
from lira.llm.prompts import check_lock


def check(report: dict[str, Any], t: dict[str, Any] | None = None) -> list[tuple[str, bool, str]]:
    t = t or config.load("release")
    g = report["golden"]
    out: list[tuple[str, bool, str]] = []

    def at_most(name: str, value: Any, limit: Any) -> None:
        out.append((name, value is not None and float(value) <= float(limit), f"{value} <= {limit}"))

    def at_least(name: str, value: Any, limit: Any) -> None:
        out.append((name, value is not None and float(value) >= float(limit), f"{value} >= {limit}"))

    rv = t["review"]
    at_most("review: money taken wrongly by automatic rules (share)", g["auto_applied"]["wrong_share"], rv["auto_applied_wrong_share_max"])
    for k, v in g["judgement"].items():
        at_least(f"review: {k} precision", v["precision"], rv["judgement_precision_min"])
        at_least(f"review: {k} recall", v["recall"], rv["judgement_recall_min"])
    at_least(
        "review: savings captured (correct proposals accepted)",
        g["money"]["accept_correct_proposals"]["captured_share"],
        rv["savings_captured_if_correct_accepted_min"],
    )
    inj = g["injection"]
    at_least("review: injections flagged", inj["flagged"] / max(1, inj["lines"]), rv["injection_flagged_min"])
    at_most("review: injections decided by the model", inj["decided_by_model"], rv["injection_decided_by_model_max"])
    at_most("review: reviewer queue lines per invoice", g["reviewer_queue_lines_per_invoice"], rv["reviewer_queue_lines_per_invoice_max"])
    c = t["compile"]
    at_least("compile: rule recall", report["compile"]["totals"]["recall"], c["recall_min"])
    at_most("compile: wrong values", report["compile"]["totals"]["wrong_values"], c["wrong_values_max"])
    at_most(
        "compile: fabricated rules accepted (faults)",
        report["compile_faults"]["fabricated_rules_accepted"],
        c["fabricated_rules_accepted_max"],
    )
    at_least("retrieval: recall@5", report["retrieval"]["recall@5"], t["retrieval"]["recall_at_5_min"])
    at_most("retrieval: cross-tenant hits", report["retrieval"]["cross_tenant_hits"], t["retrieval"]["cross_tenant_hits_max"])
    at_most("qa: unsupported answers shown", report["qa"]["unsupported_answers_shown"], t["qa"]["unsupported_answers_shown_max"])
    at_least("qa: abstained when unanswerable", report["qa"]["abstained_when_unanswerable"], t["qa"]["abstained_when_unanswerable_min"])
    f = report["faults"]
    at_most("faults: money taken wrongly by automatic rules", f["auto_applied"]["wrong_share"], t["faults"]["auto_applied_wrong_share_max"])
    at_most(
        "faults: ungrounded model findings shown", f["ungrounded_model_findings_shown"], t["faults"]["ungrounded_model_findings_shown_max"]
    )
    at_most("faults: injections decided by the model", f["injection"]["decided_by_model"], t["faults"]["injection_decided_by_model_max"])
    o = report["outage"]
    at_most("outage: invoices not reviewed", o["invoices_not_reviewed"], t["outage"]["invoices_not_reviewed_max"])
    at_most("outage: money taken wrongly by automatic rules", o["auto_applied"]["wrong_share"], t["outage"]["auto_applied_wrong_share_max"])
    at_least("drift: injected drift detected", report["drift"]["injected_drift_detected"], t["drift"]["injected_drift_detected_min"])
    at_most("appeals: closed wrongly", report["appeals"]["closed_wrongly"], t["appeals"]["closed_wrongly_max"])
    at_least("appeals: intent accuracy", report["appeals"]["intent_accuracy"], t["appeals"]["intent_accuracy_min"])
    at_most("cost: model USD per invoice", report["cost"]["model_cost_usd_per_invoice"], t["cost"]["model_cost_usd_per_invoice_max"])
    if t["prompts"]["lock_clean"]:
        problems = check_lock(config.repo_root() / "prompts.lock", config.reports_dir() / "prompts" / "history.jsonl")
        out.append(("prompts: lock and regression records", not problems, "; ".join(problems) or "clean"))
    return out


def main(report_path: Path | None = None) -> int:
    path = report_path or config.reports_dir() / "eval" / "report.json"
    report = json.loads(path.read_text())
    results = check(report)
    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    return 1 if failed else 0
