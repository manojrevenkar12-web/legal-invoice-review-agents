"""Turn the classical models' scores and the language model's decisions into findings a person decides.

Every model decision is verified before it counts: the line and question were asked, the evidence quote is in the
entry the model saw, and the cited clause is the approved rule's clause. Decisions that fail are dropped and logged
as hallucinations; the line then falls back to the classifier's view, marked as such. Instructions found in an
entry void the model's opinion on that line.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from lira import config
from lira.domain import ClauseRef, Evidence, Finding, FindingKind, Firm, Invoice, LineItem, money
from lira.guidelines.rules import BlockBilling, InternalConference, RuleSet

QUESTION_KIND = {"block_billing": FindingKind.BLOCK, "vague_description": FindingKind.VAGUE, "clerical_work": FindingKind.CLERICAL,
                 "internal_conference": FindingKind.CONFERENCE}


@dataclass
class ModelView:
    """What the model was shown for one line (to verify its quotes) and what it decided."""

    text: str
    questions: list[str]
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)  # question -> LineDecision dump
    injection_reported: str | None = None


@dataclass
class Adjudication:
    findings: list[Finding]
    dropped: list[dict[str, Any]]  # model outputs that failed verification


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_decision(d: dict[str, Any], view: ModelView, rs: RuleSet | None) -> list[str]:
    problems: list[str] = []
    if d["question"] not in view.questions:
        problems.append("answered a question that was not asked")
    q = _norm(d.get("evidence_quote", ""))
    if d["decision"] != "compliant" and (not q or q not in _norm(view.text)):
        problems.append("evidence quote is not in the entry")
    rule = rs.one(d["question"]) if rs else None
    if d["decision"] != "compliant":
        if rule is None:
            problems.append("no approved rule for this question")
        elif d.get("clause_id") != rule.clause_id:
            problems.append(f"cites clause {d.get('clause_id')!r}, the approved rule is clause {rule.clause_id}")
    return problems


def adjudicate(inv: Invoice, firm: Firm, analysis: dict[str, dict[str, Any]], rules_at: Callable[[date], RuleSet | None],
               views: dict[str, ModelView], model_failed: bool, rejected_by_rules: set[str]) -> Adjudication:
    cfg = config.load("review")
    weights = cfg["severity_weights"]
    agree, disagree = float(cfg["agreement_confidence"]), float(cfg["disagreement_confidence"])
    override = float(cfg.get("classifier_override", 0.85))
    out: list[Finding] = []
    dropped: list[dict[str, Any]] = []
    proposals: dict[tuple[str, str], tuple[float, str, dict[str, Any] | None]] = {}

    for li in inv.lines:
        if not li.is_fee or li.line_no not in analysis or li.line_no in rejected_by_rules:
            continue
        a = analysis[li.line_no]
        rs = rules_at(li.date)
        view = views.get(li.line_no)
        injected = a.get("injection") or (view.injection_reported if view else None)
        for q in ("block_billing", "vague_description", "clerical_work", "internal_conference"):
            if rs is None or rs.one(q) is None:
                continue
            p = float(a["p"].get(q, 0.0)) if q != "internal_conference" else (1.0 if a.get("conference") else 0.0)
            asked = view is not None and q in view.questions
            d = view.decisions.get(q) if view else None
            if d is not None:
                problems = verify_decision(d, view, rs)  # type: ignore[arg-type]
                if problems:
                    dropped.append({"invoice": inv.number, "line_no": li.line_no, "question": q, "decision": d, "problems": problems})
                    d = None
            if injected:
                d = None  # an entry that tries to instruct the reviewer gets no model opinion
            if d is not None:
                dec = d["decision"]
                if dec == "violation":
                    conf, why = (agree if p >= 0.5 else disagree), "model" + (" and classifier agree" if p >= 0.5 else "; classifier disagrees")
                elif dec == "unsure":
                    conf, why = disagree, "model unsure"
                elif p >= override:
                    conf, why, d = 0.4, "classifier flags it; model judged it compliant", None  # the model's quote supports nothing here
                else:
                    continue
            elif asked or model_failed or injected:
                if p < 0.5:
                    continue
                conf, why = round(p, 2), "classifier only (" + ("instruction in entry" if injected else "model output unavailable") + ")"
            else:
                continue  # not a candidate: the classifier did not think it worth a model's reading
            proposals[(li.line_no, q)] = (conf, why, d)

    # one clear reason per line: when the line already has a confident finding, weaker reasons are noise for the reviewer
    best: dict[str, float] = {}
    for (ln, _q), (conf, _w, _d) in proposals.items():
        best[ln] = max(best.get(ln, 0.0), conf)
    proposals = {k: v for k, v in proposals.items() if not (best[k[0]] >= agree and v[0] < agree)}

    # internal conferences are decided per meeting: one_attendee keeps the most senior attendee
    conf_lines = {ln for (ln, q) in proposals if q == "internal_conference"}
    keep: set[str] = set()
    for ln in conf_lines:
        li = inv.line(ln)
        rs = rules_at(li.date)
        rule = rs.one("internal_conference") if rs else None
        if isinstance(rule, InternalConference) and rule.policy == "one_attendee":
            meeting = _meeting(inv, ln, analysis)
            if len(meeting) < 2:
                keep.add(ln)  # only one side billed: nothing to reject under one_attendee
                continue
            senior = sorted(meeting, key=lambda x: (-_seniority(firm, inv.line(x)), int(x) if x.isdigit() else 0))[0]
            if ln == senior:
                keep.add(ln)
    for (ln, q), (conf, why, d) in sorted(proposals.items(), key=lambda kv: (int(kv[0][0]) if kv[0][0].isdigit() else 0, kv[0][1])):
        if q == "internal_conference" and ln in keep:
            continue
        li = inv.line(ln)
        rs = rules_at(li.date)
        rule = rs.one(q) if rs else None
        if rule is None or rs is None:
            continue
        kind = QUESTION_KIND[q]
        action, adj = "reject", -li.total
        if isinstance(rule, BlockBilling) and rule.action == "reduce" and rule.reduce_factor is not None:
            action, adj = "reduce", -money(li.total * rule.reduce_factor)
        ev = [Evidence(source="nlp", text=f"classifier p={analysis[ln]['p'].get(q, 0):.2f}" if q != "internal_conference"
                       else f"names a colleague at the firm: {', '.join(analysis[ln].get('colleagues', []))}")]
        if d is not None:
            ev.append(Evidence(source="model", text=d.get("evidence_quote", "")))
        if q == "internal_conference":
            meeting = _meeting(inv, ln, analysis)
            ev.append(Evidence(source="cross_line", text=f"same meeting: lines {', '.join(sorted(meeting, key=int))}"))
        out.append(Finding(
            id=f"{inv.number}:{ln}:{kind.value}", invoice=inv.number, line_no=ln, kind=kind, source="model" if d is not None else "nlp",
            decided_by="person", action=action, adjustment=adj,  # type: ignore[arg-type]
            clause=ClauseRef(document=rs.document, clause_id=rule.clause_id, quote=rule.quote), evidence=ev, confidence=conf,
            severity=round(float(-adj) * float(weights.get(kind.value, 1.0)) * conf, 2),
            rationale=(d.get("reason", "") if d else "") + f" [{why}]",
        ))
    return Adjudication(out, dropped)


def _meeting(inv: Invoice, ln: str, analysis: dict[str, dict[str, Any]]) -> set[str]:
    """Lines on the same day by colleagues named in this line, or naming this line's timekeeper: one meeting."""
    li = inv.line(ln)
    a = analysis[ln]
    out = {ln}
    for other in inv.lines:
        if other.line_no == ln or not other.is_fee or other.date != li.date or other.line_no not in analysis:
            continue
        b = analysis[other.line_no]
        if not b.get("conference"):
            continue
        if other.timekeeper_id in a.get("colleagues", []) or li.timekeeper_id in b.get("colleagues", []):
            out.add(other.line_no)
    return out


def _seniority(firm: Firm, li: LineItem) -> int:
    tk = firm.timekeeper(li.timekeeper_id)
    return tk.seniority if tk else -1


def flag_findings(inv: Invoice, analysis: dict[str, dict[str, Any]], rejected_by_rules: set[str]) -> list[Finding]:
    """Signals without a rule: instructions in an entry, a task code that does not match the work, unusual hours."""
    cfg = config.load("review")
    w = cfg["severity_weights"]
    out: list[Finding] = []
    for li in inv.lines:
        a = analysis.get(li.line_no)
        if a is None:
            continue
        if a.get("injection"):
            out.append(Finding(id=f"{inv.number}:{li.line_no}:prompt_injection", invoice=inv.number, line_no=li.line_no,
                               kind=FindingKind.INJECTION, source="guard", decided_by="person", action="flag",
                               evidence=[Evidence(source="guard", text=a["injection"])], confidence=1.0,
                               severity=round(float(li.total) * float(w["prompt_injection"]) * 0.25, 2),
                               rationale="The entry contains an instruction addressed to a reviewer or an AI system. "
                                         "No automated opinion on this line is applied; please review it yourself."))
        if li.line_no in rejected_by_rules:
            continue
        tc = cfg["task_code"]
        if a.get("task_code_pred") and a["task_code_pred"] != li.task_code and float(a.get("task_code_conf", 0)) >= float(tc["min_confidence"]):
            out.append(Finding(id=f"{inv.number}:{li.line_no}:task_code_mismatch", invoice=inv.number, line_no=li.line_no,
                               kind=FindingKind.TASK_CODE, source="nlp", decided_by="person", action="flag",
                               evidence=[Evidence(source="nlp", text=f"reads as {a['task_code_pred']} (p={a['task_code_conf']:.2f})")],
                               confidence=round(float(a["task_code_conf"]), 2),
                               severity=round(float(li.total) * float(w["task_code_mismatch"]) * 0.25, 2),
                               rationale=f"Billed as {li.task_code}; the description reads as {a['task_code_pred']}."))
        uz = cfg["unusual_time"]
        if float(a.get("hours_z", 0)) >= float(uz["robust_z"]) and li.units >= Decimal(str(uz["min_hours"])) and a.get("tasks", 1) < 2:
            out.append(Finding(id=f"{inv.number}:{li.line_no}:unusual_time", invoice=inv.number, line_no=li.line_no,
                               kind=FindingKind.UNUSUAL, source="nlp", decided_by="person", action="flag",
                               evidence=[Evidence(source="nlp", text=f"{li.units} h, robust z={a['hours_z']:.1f}")], confidence=0.6,
                               severity=round(float(li.total) * float(w["unusual_time"]) * 0.25, 2),
                               rationale=f"{li.units} h is far above what this task usually takes for this timekeeper level."))
    return out


def by_line(findings: list[Finding]) -> dict[str, list[Finding]]:
    out: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        if f.line_no:
            out[f.line_no].append(f)
    return out
