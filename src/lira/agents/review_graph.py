"""The invoice review workflow as a LangGraph state machine.

    parse_validate -> analyze -> apply_rules -> plan --(fan-out: one Send per batch)--> review_batch --> adjudicate
                                                    \\--------------- no candidates ---------------> adjudicate
    adjudicate -> human_review (interrupt until a person decides) -> finalize

* parse_validate: LEDES parsing and checks against the system of record. A malformed file goes back to the firm.
* analyze: classical NLP on every line (scores, entities, events, travel, injections, topics).
* apply_rules: the approved RuleSet applied by code; measurable findings are auto-applied (config/review.yaml).
* plan / review_batch: only lines where a judgement question is plausible go to the line-review agent, in parallel
  batches. The agent sees pseudonymised entries and may search the client's guidelines before recording findings.
* adjudicate: model decisions are verified and combined with the classifiers; internal conferences are resolved
  per meeting. Everything here is proposed to a person.
* human_review: LangGraph interrupt; the API resumes the run with the reviewer's decisions.
* finalize: amounts are settled again with the decisions, and each adjustment gets its explanation to the firm.

State holds only JSON-friendly values, so runs checkpoint to PostgreSQL (or SQLite locally) and resume anywhere.
"""

from __future__ import annotations

import json
import operator
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, Send, interrupt

from lira import config, ledes
from lira.agents.tools import run_tool_agent
from lira.domain import Finding, FindingKind, Invoice
from lira.guidelines.rules import RuleSet
from lira.guidelines.store import NoApprovedRules, RuleStore
from lira.llm.base import Block, Message, ModelRequest
from lira.llm.prompts import load
from lira.llm.router import Budget, ModelRouter, Unavailable
from lira.llm.schemas import RECORD_FINDINGS, SEARCH_GUIDELINES, LineReviewOutput
from lira.nlp import entities as E
from lira.nlp.analyze import analyze
from lira.nlp.models import NlpModels
from lira.obs import metrics
from lira.obs.tracing import span
from lira.reference import Reference
from lira.retrieval.index import HybridIndex, retrieve
from lira.review.adjudicate import ModelView, adjudicate, flag_findings
from lira.review.engine import Accepted, LineSignals, settle
from lira.review.explain import explain
from lira.review.validate import validate

QUESTIONS = ("block_billing", "vague_description", "clerical_work", "internal_conference")


class ReviewState(TypedDict, total=False):
    ledes_text: str
    invoice: dict[str, Any]
    tenant: str
    status: str  # "rejected_format" | "awaiting_review" | "settled"
    fatal: list[str]
    validation: list[dict[str, Any]]
    analysis: dict[str, dict[str, Any]]
    rule_findings: list[dict[str, Any]]
    spent_before: str
    batches: list[dict[str, Any]]
    views: Annotated[list[dict[str, Any]], operator.add]  # ModelView per line, from parallel batches
    model_errors: Annotated[list[str], operator.add]
    model_cost_usd: Annotated[list[float], operator.add]
    tool_calls: Annotated[list[dict[str, Any]], operator.add]
    findings: list[dict[str, Any]]
    dropped: list[dict[str, Any]]
    decisions: dict[str, dict[str, Any]]
    adjustments: dict[str, str]
    total_adjustment: str
    degraded: bool


class BatchInput(TypedDict):
    tenant: str
    tenant_name: str
    invoice: str
    entries: list[dict[str, Any]]
    clauses: list[dict[str, str]]
    on: str


@dataclass
class ReviewDeps:
    ref: Reference
    rules: RuleStore
    nlp: NlpModels
    router: ModelRouter
    index: HybridIndex
    spend_before: Callable[[Invoice], Decimal] = lambda inv: Decimal("0")
    human: Literal["interrupt", "skip"] = "interrupt"


def _rules_at(rules: RuleStore, tenant: str) -> Callable[[date], RuleSet | None]:
    cache: dict[date, RuleSet | None] = {}

    def at(d: date) -> RuleSet | None:
        if d not in cache:
            try:
                cache[d] = rules.in_force(tenant, d)
            except NoApprovedRules:
                cache[d] = None
        return cache[d]

    return at


def _dump(f: Finding) -> dict[str, Any]:
    return f.model_dump(mode="json")


def _signals(analysis: dict[str, dict[str, Any]]) -> dict[str, LineSignals]:
    return {ln: LineSignals(a.get("travel", False), a.get("event_kind"), a.get("event_key")) for ln, a in analysis.items()}


def build_review_graph(deps: ReviewDeps, checkpointer: Any = None) -> Any:
    cfg = config.load("review")
    models_cfg = config.load("models")

    # ---------------------------------------------------------------- nodes
    def parse_validate(state: ReviewState) -> dict[str, Any]:
        with span("review.parse_validate"):
            try:
                inv = ledes.parse(state["ledes_text"])
            except ledes.LedesError as e:
                return {"status": "rejected_format", "fatal": [i.message for i in e.issues]}
            v = validate(inv, deps.ref)
            if v.fatal or v.matter is None:
                return {"status": "rejected_format", "fatal": [i.message for i in v.fatal], "invoice": inv.model_dump(mode="json")}
            return {"invoice": inv.model_dump(mode="json"), "tenant": v.matter.tenant, "validation": [_dump(f) for f in v.findings],
                    "spent_before": str(deps.spend_before(inv)), "status": "in_review"}

    def after_validate(state: ReviewState) -> str:
        return END if state.get("status") == "rejected_format" else "analyze"

    def analyze_node(state: ReviewState) -> dict[str, Any]:
        inv = Invoice.model_validate(state["invoice"])
        firm = deps.ref.firms[inv.law_firm_id]
        with span("review.analyze", lines=len(inv.lines)):
            result = analyze(inv, firm, deps.nlp)
        return {"analysis": {ln: asdict(a) for ln, a in result.items()}}

    def apply_rules(state: ReviewState) -> dict[str, Any]:
        inv = Invoice.model_validate(state["invoice"])
        matter = deps.ref.matters[inv.client_matter_id]
        firm = deps.ref.firms[inv.law_firm_id]
        rules_at = _rules_at(deps.rules, matter.tenant)
        pre = {f["line_no"]: FindingKind(f["kind"]) for f in state.get("validation", []) if f["action"] == "reject" and f["line_no"]}
        with span("review.apply_rules"):
            s = settle(inv, matter, firm, deps.ref, rules_at, _signals(state["analysis"]), Decimal(state.get("spent_before", "0")),
                       pre_rejected=pre)
            flags = flag_findings(inv, state["analysis"], set(s.rejected))
        return {"rule_findings": [_dump(f) for f in s.findings + flags]}

    def plan(state: ReviewState) -> dict[str, Any]:
        inv = Invoice.model_validate(state["invoice"])
        matter = deps.ref.matters[inv.client_matter_id]
        firm = deps.ref.firms[inv.law_firm_id]
        tenant = deps.ref.tenants[matter.tenant]
        rules_at = _rules_at(deps.rules, matter.tenant)
        auto_rejected = {f["line_no"] for f in state["rule_findings"] + state.get("validation", [])
                         if f["action"] == "reject" and f["status"] == "auto_applied"}
        thresholds = cfg["candidates"]
        names: dict[str, str] = {}
        entries: list[dict[str, Any]] = []
        clause_ids: dict[str, dict[str, str]] = {}
        for li in inv.lines:
            a = state["analysis"].get(li.line_no)
            if a is None or li.line_no in auto_rejected:
                continue
            rs = rules_at(li.date)
            if rs is None:
                continue
            qs = []
            for q in QUESTIONS:
                rule = rs.one(q)
                if rule is None:
                    continue
                if q == "internal_conference":
                    plausible = a["conference"]
                else:
                    plausible = a["p"].get(q, 0.0) >= float(thresholds[q]) or (q == "block_billing" and a["tasks"] >= 2) or (
                        q == "vague_description" and len(a["normalized"].split()) <= 4)
                if plausible:
                    qs.append(q)
                    clause_ids[rule.clause_id] = {"clause_id": rule.clause_id, "text": rule.quote}
            if not qs:
                continue
            ms = [E.Mention(p["text"], p["start"], p["end"], "PERSON", p.get("timekeeper_id")) for p in a["people"]]
            text, names = E.pseudonymize(li.description, ms, names)
            tk = firm.timekeeper(li.timekeeper_id)
            people = []
            for p in a["people"]:
                ref = names.get(p.get("timekeeper_id") or p["text"].lower())
                other = firm.timekeeper(p["timekeeper_id"]) if p.get("timekeeper_id") else None
                people.append({"ref": ref, "at_firm": bool(p["at_firm"]), "role": other.title if other else "not at the firm"})
            entries.append({"line_no": li.line_no, "role": tk.title if tk else li.timekeeper_class, "task_code": li.task_code,
                            "activity_code": li.activity_code, "hours": str(li.units), "description": text, "questions": qs,
                            "people": people, "contains_instruction": bool(a.get("injection"))})
        size = int(cfg["batch_size"])
        batches = [{"tenant": matter.tenant, "tenant_name": tenant.name, "invoice": inv.number, "entries": entries[i: i + size],
                    "clauses": list(clause_ids.values()), "on": inv.period_end.isoformat() if inv.period_end else inv.date.isoformat()}
                   for i in range(0, len(entries), size)]
        return {"batches": batches}

    def fan_out(state: ReviewState) -> list[Send] | str:
        if not state.get("batches"):
            return "adjudicate"
        return [Send("review_batch", b) for b in state["batches"]]

    async def review_batch(b: BatchInput) -> dict[str, Any]:
        prompt = load("line_review")
        clauses = "\n".join(f"[{c['clause_id']}] {c['text']}" for c in b["clauses"])
        entries = json.dumps(b["entries"], indent=1)
        req = ModelRequest(task="line_review", system=prompt.system,
                           messages=[Message("user", [Block.t(prompt.render_user(tenant_name=b["tenant_name"], clauses=clauses, entries=entries))])],
                           tools=[SEARCH_GUIDELINES, RECORD_FINDINGS], max_tokens=4096, prompt_id=prompt.id,
                           prompt_version=prompt.version, tenant=b["tenant"], metadata={"invoice": b["invoice"]})
        on = date.fromisoformat(b["on"])

        async def search(args: dict[str, Any]) -> dict[str, Any]:
            hits = retrieve(deps.index, str(args.get("query", "")), b["tenant"], k=int(args.get("limit", 3)), on=on)
            return {"clauses": [{"clause_id": h.chunk.clause_id, "text": h.chunk.text} for h in hits]}

        budget = Budget(float(models_cfg["routing"]["max_cost_usd_per_invoice"]))
        with span("review.review_batch", entries=len(b["entries"])):
            try:
                run = await run_tool_agent(deps.router, req, {"search_guidelines": search}, "record_findings",
                                           LineReviewOutput.model_validate, int(cfg["max_agent_steps"]), budget)
            except Unavailable as e:
                metrics.emit({"node": "review_batch", "outcome": "degraded"}, batches=1)
                return {"model_errors": [str(e)], "views": [
                    asdict(ModelView(e_["description"], e_["questions"])) | {"line_no": e_["line_no"], "failed": True} for e_ in b["entries"]]}
        out = run.value
        views: dict[str, ModelView] = {e_["line_no"]: ModelView(e_["description"], e_["questions"]) for e_ in b["entries"]}
        for d in out.findings:
            if d.line_no in views:
                views[d.line_no].decisions[d.question] = d.model_dump()
        for inj in out.injections:
            if inj.line_no in views:
                views[inj.line_no].injection_reported = inj.quote
        return {"views": [asdict(v) | {"line_no": ln, "failed": False} for ln, v in views.items()],
                "model_cost_usd": [run.cost_usd], "tool_calls": run.tool_calls}

    def adjudicate_node(state: ReviewState) -> dict[str, Any]:
        inv = Invoice.model_validate(state["invoice"])
        matter = deps.ref.matters[inv.client_matter_id]
        firm = deps.ref.firms[inv.law_firm_id]
        rules_at = _rules_at(deps.rules, matter.tenant)
        views: dict[str, ModelView] = {}
        failed = False
        for v in state.get("views", []):
            if v.get("failed"):
                failed = True
                continue
            views[v["line_no"]] = ModelView(v["text"], v["questions"], v["decisions"], v.get("injection_reported"))
        rejected = {f["line_no"] for f in state["rule_findings"] + state.get("validation", [])
                    if f["action"] == "reject" and f["status"] == "auto_applied" and f["line_no"]}
        with span("review.adjudicate"):
            adj = adjudicate(inv, firm, state["analysis"], rules_at, views, failed or bool(state.get("model_errors")), rejected)
        if adj.dropped:
            metrics.emit({"node": "adjudicate", "outcome": "hallucination"}, dropped=len(adj.dropped))
        reported = _injections_reported(inv, views, state["analysis"])
        findings = [*state.get("validation", []), *state["rule_findings"], *(_dump(f) for f in adj.findings + reported)]
        findings.sort(key=lambda f: -float(f.get("severity") or 0))
        return {"findings": findings, "dropped": adj.dropped, "degraded": failed or bool(state.get("model_errors"))}

    def route_human(state: ReviewState) -> str:
        pending = [f for f in state["findings"] if f["status"] == "proposed"]
        if pending and deps.human == "interrupt":
            return "human_review"
        return "finalize"

    def human_review(state: ReviewState) -> dict[str, Any]:
        pending = [f["id"] for f in state["findings"] if f["status"] == "proposed"]
        decisions = interrupt({"invoice": state["invoice"]["number"], "pending": pending})
        return {"decisions": decisions or {}}

    def finalize(state: ReviewState) -> dict[str, Any]:
        inv = Invoice.model_validate(state["invoice"])
        matter = deps.ref.matters[inv.client_matter_id]
        firm = deps.ref.firms[inv.law_firm_id]
        tenant = deps.ref.tenants[matter.tenant]
        rules_at = _rules_at(deps.rules, matter.tenant)
        decisions = state.get("decisions") or {}
        findings = [Finding.model_validate(f) for f in state["findings"]]
        accepted: dict[str, list[Accepted]] = {}
        dismissed: set[str] = set()
        confirmed: set[str] = {fid for fid, d in decisions.items() if d["decision"] == "accept"}
        for f in findings:
            d = decisions.get(f.id)
            if d is None:
                continue
            if d["decision"] == "reject":
                dismissed.add(f.id)
            elif d["decision"] == "accept" and f.decided_by == "person" and f.line_no and f.action in ("reject", "reduce") and f.source != "rule":
                factor = (-f.adjustment / _base(f, inv)) if f.action == "reduce" else None
                accepted.setdefault(f.line_no, []).append(Accepted(f.kind, f.action, factor))
        pre = {f.line_no: f.kind for f in findings if f.source == "validation" and f.action == "reject" and f.line_no and f.id not in dismissed}
        uncertain = {f.line_no for f in findings if f.line_no and f.status == "proposed" and f.action in ("reject", "reduce")
                     and f.id not in decisions and f.kind not in (FindingKind.DAILY_HOURS, FindingKind.RESEARCH)}
        s = settle(inv, matter, firm, deps.ref, rules_at, _signals(state["analysis"]), Decimal(state.get("spent_before", "0")),
                   accepted=accepted, pre_rejected=pre, dismissed=dismissed, confirmed=confirmed, uncertain=uncertain)
        settled = {f.id: f for f in s.findings}
        known = {f.id for f in findings}
        findings += [f for f in s.findings if f.id not in known]  # findings that only appear once decisions are in
        out: list[dict[str, Any]] = []
        for f in findings:
            g = settled.get(f.id, f)
            d = decisions.get(f.id)
            status = g.status
            if d is not None:
                status = "accepted" if d["decision"] == "accept" else "rejected"
            elif f.status == "proposed":
                status = "proposed"
            li = inv.line(f.line_no) if f.line_no else None
            dump = _dump(g) | {"status": status, "explanation": explain(g, li, tenant.currency)}
            if d is not None:
                dump["decision"] = d
            out.append(dump)
        pending = any(f["status"] == "proposed" for f in out)
        return {"findings": out, "adjustments": {k: str(v) for k, v in s.adjustments.items()},
                "total_adjustment": str(s.total_adjustment), "status": "awaiting_review" if pending else "settled"}

    # ---------------------------------------------------------------- graph
    g = StateGraph(ReviewState)
    g.add_node("parse_validate", parse_validate)
    g.add_node("analyze", analyze_node)
    g.add_node("apply_rules", apply_rules)
    g.add_node("plan", plan)
    g.add_node("review_batch", review_batch, retry_policy=RetryPolicy(max_attempts=2))
    g.add_node("adjudicate", adjudicate_node)
    g.add_node("human_review", human_review)
    g.add_node("finalize", finalize)
    g.add_edge(START, "parse_validate")
    g.add_conditional_edges("parse_validate", after_validate, ["analyze", END])
    g.add_edge("analyze", "apply_rules")
    g.add_edge("apply_rules", "plan")
    g.add_conditional_edges("plan", fan_out, ["review_batch", "adjudicate"])
    g.add_edge("review_batch", "adjudicate")
    g.add_conditional_edges("adjudicate", route_human, ["human_review", "finalize"])
    g.add_edge("human_review", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def _injections_reported(inv: Invoice, views: dict[str, ModelView], analysis: dict[str, dict[str, Any]]) -> list[Finding]:
    """Instructions the model found that the pattern guard missed."""
    out = []
    for ln, v in views.items():
        if v.injection_reported and not analysis.get(ln, {}).get("injection"):
            li = inv.line(ln)
            out.append(Finding(id=f"{inv.number}:{ln}:prompt_injection", invoice=inv.number, line_no=ln, kind=FindingKind.INJECTION,
                               source="model", decided_by="person", action="flag", confidence=0.7,
                               severity=round(float(li.total) * 0.5, 2), rationale="The model reported an instruction inside this entry.",
                               evidence=[]))
    return out


def _base(f: Finding, inv: Invoice) -> Decimal:
    li = inv.line(f.line_no) if f.line_no else None
    return li.total if li and li.total else Decimal("1")

