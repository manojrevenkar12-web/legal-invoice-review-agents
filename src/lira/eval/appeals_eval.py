"""Appeals against the system's own proposals, generated from the answer key, run through the appeal workflow."""

from __future__ import annotations

from typing import Any

import numpy as np

from lira.agents.appeal_graph import AppealDeps, build_appeal_graph
from lira.nlp.models import NlpModels
from lira.llm.router import ModelRouter
from lira.reference import Reference
from lira.sim.appeals import appeal_for


async def evaluate(runs: list[Any], truth: dict[tuple[str, str], dict[str, Any]], ref: Reference, nlp: NlpModels, router: ModelRouter,
                   n: int = 120, seed: int = 3) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    pool = [(run, f) for run in runs for f in run.findings
            if f.line_no and f.status == "proposed" and f.kind.value in ("block_billing", "vague_description", "clerical_work",
                                                                         "internal_conference", "travel_time")]
    picks = [pool[i] for i in rng.choice(len(pool), size=min(n, len(pool)), replace=False)] if pool else []
    graph = build_appeal_graph(AppealDeps(nlp, router, human="skip"))
    rows = []
    for run, f in picks:
        inv = run.invoice
        tk = {k["kind"] for k in truth[(inv.number, f.line_no)]["kinds"]}
        ap = appeal_for(f.model_dump(mode="json"), tk, rng)
        matter = ref.matters[inv.client_matter_id]
        tenant = ref.tenants[matter.tenant]
        state = await graph.ainvoke({"appeal": {
            "message": ap["message"], "finding": f.model_dump(mode="json"), "description": inv.line(f.line_no).description,
            "firm_name": ref.firms[inv.law_firm_id].name, "tenant": tenant.id, "tenant_name": tenant.name, "currency": tenant.currency}})
        rec = (state.get("recommendation") or {}).get("recommendation")
        rows.append({"intent": ap["intent"], "predicted_intent": state["intent"], "justified": ap["justified"], "recommendation": rec,
                     "outcome": state.get("outcome"), "problems": state.get("problems", [])})
    assessed = [r for r in rows if r["recommendation"]]
    right = [r for r in assessed if (r["recommendation"] == "reverse") == r["justified"]]
    rev = [r for r in assessed if r["recommendation"] == "reverse"]
    just = [r for r in rows if r["justified"]]
    return {
        "appeals": len(rows),
        "intent_accuracy": round(sum(r["intent"] == r["predicted_intent"] for r in rows) / max(1, len(rows)), 4),
        "closed_without_model": sum(r["outcome"] in ("closed_accepted", "answered") for r in rows),
        "closed_wrongly": sum(r["outcome"] == "closed_accepted" and r["justified"] for r in rows),
        "assessed_by_model": len(assessed),
        "recommendation_accuracy": round(len(right) / max(1, len(assessed)), 4),
        "reverse_precision": round(sum(r["justified"] for r in rev) / max(1, len(rev)), 4) if rev else None,
        "justified_appeals": len(just),
        "justified_recommended_reverse": sum(1 for r in just if r["recommendation"] == "reverse"),
        "blocked_by_verification": sum(1 for r in rows if r["problems"] and r["recommendation"] is None),
    }
