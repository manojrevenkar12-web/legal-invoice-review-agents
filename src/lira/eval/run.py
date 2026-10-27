"""Run every evaluation and write reports/eval/report.json and reports/eval/SUMMARY.md.

    golden   the live months (August to October 2026) reviewed with the simulated model
    faults   the same months with a model that returns malformed output, invents clauses and quotes and obeys injections
    outage   the same months with the model down for the whole run
plus guideline compilation, retrieval and Q&A, drift, appeals and the LLM-as-judge against annotators.
Only lira.eval (and lira.sim) read data/truth.
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from lira import config, ledes
from lira.agents.review_graph import ReviewDeps
from lira.context import Context
from lira.eval import appeals_eval, bedrock_export, compile_eval, judge, retrieval_eval, review_eval
from lira.llm.prompts import all_prompts
from lira.llm.router import ModelRouter
from lira.llm.simulated import SimulatedProvider
from lira.monitor.drift import monitor

FAULTS = {"invalid": 0.3, "hallucinate": 0.3, "fabricate_quote": 0.3, "obey_injection": 1.0, "flip": 0.1}


def router_with(faults: dict[str, float]) -> ModelRouter:
    return ModelRouter({"simulated": SimulatedProvider(faults)}, sleep=_nosleep)


async def _nosleep(_s: float) -> None:
    return None


def live_files(data: Path, first_live: str) -> list[Path]:
    return [p for p in sorted((data / "world" / "invoices").glob("*/*/*.ledes")) if p.parent.name >= first_live]


def grounded(f: Any, description: str) -> bool:
    """A model-sourced finding shown to a reviewer must quote the entry (names were masked when the model saw it)."""
    quote = next((e.text for e in f.evidence if e.source == "model"), None)
    if quote is None:
        return True
    parts = [p.strip().lower() for p in re.split(r"PERSON_\d+", quote) if p.strip()]
    d = re.sub(r"\s+", " ", description).lower()
    return all(re.sub(r"\s+", " ", p) in d for p in parts)


def safety(runs: list[review_eval.Run]) -> dict[str, Any]:
    shown = ungrounded = 0
    for run in runs:
        for f in run.findings:
            if f.source == "model" and f.line_no:
                shown += 1
                ungrounded += not grounded(f, run.invoice.line(f.line_no).description)
    not_reviewed = sum(1 for r in runs if r.state.get("status") not in ("awaiting_review", "settled", "rejected_format"))
    return {"model_findings_shown": shown, "ungrounded_model_findings_shown": ungrounded, "invoices_not_reviewed": not_reviewed}


async def run_all(data: Path | None = None, reports: Path | None = None, quick: bool = False) -> dict[str, Any]:
    data = data or config.data_dir()
    reports = reports or config.reports_dir()
    world = json.loads((data / "world" / "world.json").read_text())
    y, m = map(int, world["first_month"].split("-"))
    k = y * 12 + m - 1 + world["history_months"]
    first_live = f"{k // 12}-{k % 12 + 1:02d}"
    t0 = time.perf_counter()
    ctx = Context.load(data=data)
    truth_lines, truth_invoices = review_eval.load_truth(data)
    answers = json.loads((data / "truth" / "guidelines.json").read_text())
    names = {t: x.name for t, x in ctx.ref.tenants.items()}
    files = live_files(data, first_live)
    if quick:
        files = files[::3]
    report: dict[str, Any] = {"generated_at": date.today().isoformat(), "model": "simulated", "live_from": first_live,
                              "invoices_reviewed": len(files)}

    # ---- golden
    deps = ReviewDeps(ctx.ref, ctx.rules, ctx.nlp, router_with({}), ctx.index)
    t1 = time.perf_counter()
    runs = await review_eval.run_reviews(deps, files)
    elapsed = time.perf_counter() - t1
    report["golden"] = review_eval.score(runs, truth_lines, truth_invoices, deps) | safety(runs)
    report["golden"]["seconds_per_invoice"] = round(elapsed / max(1, len(files)), 3)

    # ---- faults and outage
    for name, faults in (("faults", FAULTS), ("outage", {"outage": 1.0})):
        d = ReviewDeps(ctx.ref, ctx.rules, ctx.nlp, router_with(faults), ctx.index)
        r = await review_eval.run_reviews(d, files)
        s = review_eval.score(r, truth_lines, truth_invoices, d) | safety(r)
        report[name] = {"faults": faults, **{k2: s[k2] for k2 in ("auto_applied", "money", "judgement", "injection", "model_outputs",
                                                                  "degraded_invoices", "model_findings_shown",
                                                                  "ungrounded_model_findings_shown", "invoices_not_reviewed",
                                                                  "reviewer_queue_lines_per_invoice")}}
        report[name]["auto_applied"].pop("lines_wrong", None)

    # ---- guideline compilation (clean and with a model that invents rules)
    docs = [(ctx.docs.get(t, v), names[t]) for t in ctx.docs.tenants() for v in ctx.docs.versions(t)]
    report["compile"] = await compile_eval.evaluate(docs, answers, router_with({}))
    bad = await compile_eval.evaluate(docs, answers, router_with({"hallucinate": 1.0, "fabricate_quote": 1.0}))
    accepted_fabrications = sum(len(d["extra"]) + len(d["wrong_values"]) for d in bad["documents"].values())
    report["compile_faults"] = {"rejected_by_verifier": bad["totals"]["rejected_by_verifier"],
                                "fabricated_rules_accepted": accepted_fabrications}

    # ---- retrieval and Q&A
    report["retrieval"] = retrieval_eval.retrieval_only(ctx.index, answers)
    report["qa"] = await retrieval_eval.qa(ctx.index, router_with({}), answers, names)

    # ---- drift
    rows = []
    for p in sorted((data / "world" / "invoices").glob("*/*/*.ledes")):
        inv = ledes.parse(p.read_text())
        rows += [{"firm": inv.law_firm_id, "month": f"{li.date:%Y-%m}", "description": li.description, "activity_code": li.activity_code,
                  "timekeeper_class": li.timekeeper_class, "units": float(li.units)} for li in inv.lines if li.is_fee]
    ref_first = f"{(k - 2) // 12}-{(k - 2) % 12 + 1:02d}"
    ref_last = f"{(k - 1) // 12}-{(k - 1) % 12 + 1:02d}"
    drift = monitor(rows, ctx.nlp, (ref_first, ref_last), emit=False)
    injected = json.loads((data / "truth" / "drift.json").read_text())
    expected = {(firm, w["month"]) for firm, start in injected.items() for w in drift["windows"] if w["firm"] == firm and w["month"] >= start}
    alerts = {(w["firm"], w["month"]) for w in drift["alerts"]}
    report["drift"] = {"injected": sorted(map(list, expected)), "alerts": sorted(map(list, alerts)),
                       "injected_drift_detected": round(len(expected & alerts) / max(1, len(expected)), 4),
                       "other_alerts": [w | {"psi": {k3: v for k3, v in w["psi"].items() if v >= 0.25}} for w in drift["alerts"]
                                        if (w["firm"], w["month"]) not in expected],
                       "windows": len(drift["windows"])}
    report["drift"]["quality_by_firm"] = _quality_by_firm(runs, truth_lines, injected)

    # ---- appeals
    report["appeals"] = await appeals_eval.evaluate(runs, truth_lines, ctx.ref, ctx.nlp, router_with({}))

    # ---- LLM-as-judge and the annotation round
    ann = config.repo_root() / "evals" / "annotation" / "round-1"
    items_path = ann / "items.jsonl"
    if not items_path.exists():
        items = judge.sample_items(runs, {}, n=60)
        bedrock_export.write_jsonl(items_path, items)
    items = [json.loads(x) for x in items_path.read_text().splitlines() if x.strip()]
    verdicts, bias = await judge.run_judge(router_with({}), items)
    a, b = judge.load_labels(ann / "annotator_a.jsonl"), judge.load_labels(ann / "annotator_b.jsonl")
    report["judge"] = {"items": len(items), **bias, "annotations": bool(a and b),
                       "agreement": judge.agreement(items, a, b, verdicts) if a and b else None,
                       "judge_vs_answer_key": _judge_vs_truth(items, verdicts, truth_lines)}

    # ---- cost
    report["cost"] = {"model_cost_usd_per_invoice": report["golden"]["model_cost_usd_per_invoice"],
                      "note": "simulated model: token counts are estimated and priced at the configured Bedrock rates"}
    report["prompts"] = {p.id: {"version": p.version, "sha256": p.sha256[:16]} for p in all_prompts()}

    # ---- Bedrock Evaluations exports
    bedrock_export.write_jsonl(reports / "bedrock" / "findings_byoi.jsonl", bedrock_export.finding_records(items))
    (reports / "bedrock").mkdir(parents=True, exist_ok=True)
    (reports / "bedrock" / "custom_metric_grounded_in_clause.json").write_text(json.dumps(bedrock_export.GROUNDED_METRIC, indent=2))

    report["seconds"] = round(time.perf_counter() - t0, 1)
    out = reports / "eval"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    (out / "SUMMARY.md").write_text(summary(report))
    _record_prompt_runs(reports, report)
    return report


def _quality_by_firm(runs: list[review_eval.Run], truth: dict[tuple[str, str], dict[str, Any]], injected: dict[str, str]) -> dict[str, Any]:
    """Judgement recall per firm before and after its drift month: why drift matters."""
    out: dict[str, Any] = {}
    for firm, start in injected.items():
        rows: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for run in runs:
            if run.invoice.law_firm_id != firm:
                continue
            found = {(f.line_no, f.kind.value) for f in run.findings}
            for li in run.invoice.lines:
                for kk in {x["kind"] for x in truth[(run.invoice.number, li.line_no)]["kinds"]} & {"block_billing", "vague_description",
                                                                                                    "clerical_work"}:
                    key = "after" if f"{li.date:%Y-%m}" >= start else "before"
                    rows[key][0] += (li.line_no, kk) in found
                    rows[key][1] += 1
        out[firm] = {k: {"found": v[0], "lines": v[1], "recall": round(v[0] / v[1], 4) if v[1] else None} for k, v in rows.items()}
    return out


def _judge_vs_truth(items: list[dict[str, Any]], verdicts: dict[str, dict[str, str]], truth: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    agree = n = 0
    for it in items:
        v = verdicts.get(it["id"])
        if v is None:
            continue
        inv = it["finding_id"].split(":")[0]
        kinds = {k["kind"] for k in truth.get((inv, it["line_no"]), {}).get("kinds", [])}
        n += 1
        agree += (v["correct"] == "pass") == (it["kind"] in kinds)
    return {"items": n, "correct_criterion_matches_answer_key": round(agree / n, 4) if n else None}


def _record_prompt_runs(reports: Path, report: dict[str, Any]) -> None:
    """Append one regression record per prompt version (deduplicated by sha), used by `lira prompts check`."""
    g = report["golden"]["judgement"]
    f1 = [v["f1"] for v in g.values() if v.get("f1") is not None]
    metric = {
        "line_review": {"judgement_f1_mean": round(sum(f1) / len(f1), 4) if f1 else None},
        "guideline_compile": {"recall": report["compile"]["totals"]["recall"]},
        "qa_answer": {"answered_with_right_clause": report["qa"]["answered_with_right_clause"]},
        "appeal_review": {"recommendation_accuracy": report["appeals"]["recommendation_accuracy"]},
        "judge_finding": {"correct_matches_answer_key": report["judge"]["judge_vs_answer_key"]["correct_criterion_matches_answer_key"]},
    }
    path = reports / "prompts" / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if path.exists():
        for line in path.read_text().splitlines():
            r = json.loads(line)
            seen.add((r["prompt_id"], r["version"], r["sha256"]))
    with path.open("a") as fh:
        for p in all_prompts():
            if (p.id, p.version, p.sha256) in seen:
                continue
            fh.write(json.dumps({"prompt_id": p.id, "version": p.version, "sha256": p.sha256, "date": report["generated_at"],
                                 "model": report["model"], "metrics": metric.get(p.id, {})}) + "\n")


def _fmt(x: Any) -> str:
    if isinstance(x, float):
        return f"{x:.1%}" if 0 <= x <= 1 else f"{x:,.2f}"
    return str(x)


def summary(r: dict[str, Any]) -> str:
    g = r["golden"]
    j = g["judgement"]
    lines = [
        "# Evaluation summary",
        "",
        f"Generated by `lira eval` on {r['generated_at']} with the simulated model; {r['invoices_reviewed']} invoices from "
        f"{r['live_from']} onwards. Every number below is in `report.json`; this file is generated, not written.",
        "",
        "## Invoice review",
        "",
        "| | |",
        "| --- | --- |",
        f"| Money taken automatically | {g['auto_applied']['money_taken']} (wrongly: {g['auto_applied']['money_taken_wrongly']}) |",
        f"| Savings captured if the reviewer accepts only correct proposals | {_fmt(g['money']['accept_correct_proposals']['captured_share'])} |",
        f"| Savings captured if the reviewer accepts every proposal | {_fmt(g['money']['accept_all_proposals']['captured_share'])} "
        f"(taken wrongly: {g['money']['accept_all_proposals']['taken_wrongly']}) |",
        f"| Lines sent to the language model | {_fmt(g['share_sent_to_model'])} |",
        f"| Reviewer queue | {g['reviewer_queue_lines_per_invoice']} lines per invoice |",
        f"| Entries with instructions to an AI | {g['injection']['flagged']} of {g['injection']['lines']} flagged; "
        f"{g['injection']['decided_by_model']} decided by the model |",
        "",
        "| Judgement finding | Precision | Recall |",
        "| --- | --- | --- |",
        *[f"| {k} | {_fmt(v['precision'])} | {_fmt(v['recall'])} |" for k, v in j.items()],
        "",
        "## Under faults and outage",
        "",
        f"- Faulty model ({', '.join(f'{k}={v}' for k, v in r['faults']['faults'].items())}): money taken wrongly by automatic "
        f"rules {r['faults']['auto_applied']['money_taken_wrongly']}; model findings shown without grounding "
        f"{r['faults']['ungrounded_model_findings_shown']}; model outputs stopped by the verifier "
        f"{r['faults']['model_outputs']['dropped_by_verifier']} of {r['faults']['model_outputs']['decisions']}; "
        f"injections decided by the model {r['faults']['injection']['decided_by_model']}.",
        f"- Model down: invoices not reviewed {r['outage']['invoices_not_reviewed']}; degraded {r['outage']['degraded_invoices']}; "
        f"savings captured if correct proposals accepted {_fmt(r['outage']['money']['accept_correct_proposals']['captured_share'])}.",
        "",
        "## Guidelines, retrieval, drift, appeals, judge",
        "",
        f"- Compilation: {r['compile']['totals']['found']} of {r['compile']['totals']['expected']} rules, "
        f"{r['compile']['totals']['wrong_values']} wrong values; with a model inventing rules, "
        f"{r['compile_faults']['rejected_by_verifier']} proposals rejected and {r['compile_faults']['fabricated_rules_accepted']} accepted.",
        f"- Retrieval: recall@5 {_fmt(r['retrieval']['recall@5'])}, MRR {r['retrieval']['mrr']}, cross-tenant hits "
        f"{r['retrieval']['cross_tenant_hits']}. Q&A: right clause {_fmt(r['qa']['answered_with_right_clause'])}, abstained on "
        f"unanswerable {_fmt(r['qa']['abstained_when_unanswerable'])}, unsupported answers shown {r['qa']['unsupported_answers_shown']}.",
        f"- Drift: injected drift detected {_fmt(r['drift']['injected_drift_detected'])}; other alerts "
        f"{len(r['drift']['other_alerts'])} ({', '.join(a['firm'] + ' ' + a['month'] + ': ' + '/'.join(a['drivers']) for a in r['drift']['other_alerts']) or 'none'}).",
        f"- Appeals: intent accuracy {_fmt(r['appeals']['intent_accuracy'])}, recommendation accuracy "
        f"{_fmt(r['appeals']['recommendation_accuracy'])}, closed wrongly {r['appeals']['closed_wrongly']}.",
        f"- Judge: {r['judge']['judged']} items; verdict changed by padding the explanation {r['judge']['length_padding_flips']} times; "
        f"'correct' matches the answer key {_fmt(r['judge']['judge_vs_answer_key']['correct_criterion_matches_answer_key'])}.",
    ]
    if r["judge"].get("agreement"):
        ag = r["judge"]["agreement"]
        lines += ["", "| Criterion | Annotators kappa | Judge vs adjudicated kappa |", "| --- | --- | --- |",
                  *[f"| {c} | {ag[c]['annotators_kappa']} | {ag[c]['judge_vs_gold_kappa']} |" for c in judge.CRITERIA]]
    return "\n".join(lines) + "\n"


def _d(x: Any) -> Decimal:
    return Decimal(str(x))
