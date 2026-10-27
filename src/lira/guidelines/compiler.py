"""Compile a guidelines document into a draft RuleSet for legal operations to approve.

The model proposes rules through a forced tool call. Code then checks each one: the clause exists, the quote is
verbatim in it, the values are stated in the quote. Rules that fail are shown to the approver with the reason and
never reach the RuleSet. A pattern extractor runs as a second opinion; where the two differ, the approver sees it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import TypeAdapter, ValidationError

from lira.guidelines.documents import GuidelineDocument
from lira.guidelines.extract import extract
from lira.guidelines.rules import Rule, RuleSet, verify
from lira.llm.base import Block, InvalidOutput, Message, ModelRequest, ModelResponse
from lira.llm.prompts import load
from lira.llm.router import Budget, ModelRouter
from lira.llm.schemas import RECORD_RULES

_RULE = TypeAdapter(Rule)
_NUMERIC = ("hours", "rate_factor", "reduce_factor", "max_unit_cost", "hours_per_month")
_FIELDS = {
    "billing_increment": ("hours",), "max_daily_hours": ("hours",), "travel_time": ("rate_factor",),
    "internal_conference": ("policy",), "block_billing": ("action", "reduce_factor"), "vague_description": (),
    "clerical_work": (), "nonbillable_role": ("roles",), "nonreimbursable_expense": ("codes",),
    "expense_unit_cap": ("code", "max_unit_cost"), "submission_deadline": ("days",), "max_attendees": ("event", "max_timekeepers"),
    "research_cap": ("hours_per_month",), "rate_cap": (), "budget": (),
}


@dataclass
class CompileReport:
    ruleset: RuleSet
    rejected: list[dict[str, Any]] = field(default_factory=list)  # proposals that failed verification, with reasons
    disagreements: list[dict[str, Any]] = field(default_factory=list)  # model vs pattern extractor
    changes: list[dict[str, Any]] = field(default_factory=list)  # versus the previous approved version
    model: str = ""
    cost_usd: float = 0.0


def to_rule(d: dict[str, Any]) -> Rule:
    kind = d.get("kind")
    if kind not in _FIELDS:
        raise ValueError(f"unknown rule kind {kind!r}")
    data: dict[str, Any] = {"kind": kind, "clause_id": str(d.get("clause_id", "")), "quote": str(d.get("quote", ""))}
    for f in _FIELDS[kind]:
        if f in d and d[f] is not None:
            v = d[f]
            data[f] = Decimal(str(v)) if f in _NUMERIC else (tuple(v) if isinstance(v, list) else v)
    if kind == "block_billing" and data.get("action") == "reject":
        data.pop("reduce_factor", None)
    return _RULE.validate_python(data)


def _parse(resp: ModelResponse) -> list[dict[str, Any]]:
    payload = resp.tool_input("record_rules")
    rules = payload.get("rules")
    if not isinstance(rules, list):
        raise InvalidOutput("record_rules.rules must be a list")
    return [r for r in rules if isinstance(r, dict)]


def clauses_block(doc: GuidelineDocument) -> str:
    return "\n".join(f"[{c.id}] {c.text}" for c in doc.clauses)


async def compile_guidelines(doc: GuidelineDocument, router: ModelRouter, tenant_name: str, previous: RuleSet | None = None,
                             budget: Budget | None = None) -> CompileReport:
    prompt = load("guideline_compile")
    req = ModelRequest(
        task="guideline_compile", system=prompt.system,
        messages=[Message("user", [Block.t(prompt.render_user(tenant_name=tenant_name, document=doc.ref, clauses=clauses_block(doc)))])],
        tools=[RECORD_RULES], tool_choice="record_rules", max_tokens=4096, prompt_id=prompt.id, prompt_version=prompt.version,
        tenant=doc.tenant,
    )
    routed = await router.call(req, _parse, budget)
    accepted: list[Rule] = []
    rejected: list[dict[str, Any]] = []
    keys: set[tuple[Any, ...]] = set()
    rs = RuleSet(tenant=doc.tenant, version=doc.version, document=doc.ref, document_sha256=doc.sha256,
                 effective_from=doc.effective_from, rules=[], compiled_by=f"{routed.model.key}:{prompt.id}@v{prompt.version}")
    for proposal in routed.value:
        try:
            rule = to_rule(proposal)
        except (ValidationError, ValueError) as e:
            rejected.append({"proposal": proposal, "problems": [f"invalid rule: {str(e)[:200]}"]})
            continue
        clause = doc.clause(rule.clause_id)
        problems = ["cited clause does not exist"] if clause is None else verify(rule, clause.text)
        if problems:
            rejected.append({"proposal": proposal, "problems": problems})
            continue
        k = rs.key(rule)
        if k in keys:
            rejected.append({"proposal": proposal, "problems": ["duplicate of an accepted rule"]})
            continue
        keys.add(k)
        accepted.append(rule)
    rs.rules = accepted
    report = CompileReport(rs, rejected, model=routed.model.key, cost_usd=routed.cost_usd)
    report.disagreements = cross_check(rs, [to_rule(r) for r in extract(doc.clauses)])
    if previous is not None:
        report.changes = diff(previous, rs)
    return report


def _params(r: Rule) -> dict[str, Any]:
    return {k: (format(v.normalize(), "f") if isinstance(v, Decimal) else list(v) if isinstance(v, tuple) else v)
            for k, v in r.model_dump().items() if k not in ("clause_id", "quote")}


def cross_check(model_rules: RuleSet, pattern_rules: list[Rule]) -> list[dict[str, Any]]:
    m = {model_rules.key(r): r for r in model_rules.rules}
    p = {model_rules.key(r): r for r in pattern_rules}
    out = []
    for k in sorted(set(m) | set(p), key=str):
        a, b = m.get(k), p.get(k)
        if a is None or b is None or _params(a) != _params(b):
            out.append({"rule": list(map(str, k)), "model": _params(a) if a else None, "patterns": _params(b) if b else None})
    return out


def diff(old: RuleSet, new: RuleSet) -> list[dict[str, Any]]:
    a = {old.key(r): r for r in old.rules}
    b = {new.key(r): r for r in new.rules}
    out: list[dict[str, Any]] = []
    for k in sorted(set(a) | set(b), key=str):
        if k not in a:
            out.append({"change": "added", "rule": list(map(str, k)), "now": _params(b[k]), "clause": b[k].clause_id})
        elif k not in b:
            out.append({"change": "removed", "rule": list(map(str, k)), "was": _params(a[k])})
        elif _params(a[k]) != _params(b[k]):
            out.append({"change": "changed", "rule": list(map(str, k)), "was": _params(a[k]), "now": _params(b[k]), "clause": b[k].clause_id})
    return out
