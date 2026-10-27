"""Appeals and guideline compilation under a well-behaved and a misbehaving model."""

from __future__ import annotations

import json

from lira.agents.appeal_graph import AppealDeps, build_appeal_graph
from lira.guidelines.compiler import compile_guidelines, diff

from .conftest import router_with

FINDING = {
    "id": "INV:4:internal_conference",
    "kind": "internal_conference",
    "line_no": "4",
    "adjustment": "-319.50",
    "rationale": "Meeting with a colleague at the same firm.",
    "clause": {"document": "kestrel/guidelines@v2", "clause_id": "4.2", "quote": "only the most senior attendee may bill"},
}


def appeal(message: str) -> dict:
    return {
        "appeal": {
            "message": message,
            "finding": FINDING,
            "description": "Discuss case status with PERSON_1",
            "firm_name": "Okafor",
            "tenant": "kestrel",
            "tenant_name": "Kestrel",
            "currency": "EUR",
        }
    }


async def test_appeals_route_by_intent_and_recommendations_quote_the_appeal(ctx):
    g = build_appeal_graph(AppealDeps(ctx.nlp, router_with({}), human="skip"))
    accepted = await g.ainvoke(appeal("We accept the adjustment on line 4 and will issue a corrected invoice."))
    assert accepted["outcome"] == "closed_accepted" and accepted.get("recommendation") is None
    info = await g.ainvoke(appeal("Which provision applies to line 4? We could not find it."))
    assert info["outcome"] == "answered" and "clause 4.2" in info["reply"]
    facts = await g.ainvoke(
        appeal("The meeting recorded on line 4 was with Katrin Albers, the client's in-house counsel, not with a colleague at our firm.")
    )
    assert facts["recommendation"]["recommendation"] == "reverse"
    assert (
        facts["recommendation"]["appeal_quote"] in appeal("x")["appeal"]["message"]
        or "in-house counsel" in facts["recommendation"]["appeal_quote"]
    )
    policy = await g.ainvoke(appeal("We have always billed this way on your matters and it has never been questioned before."))
    assert policy["recommendation"]["recommendation"] == "uphold" and policy["outcome"] == "awaiting_decision"


async def test_a_misquoting_appeal_agent_is_not_shown_to_the_reviewer(ctx):
    class Misquote:
        name = "simulated"

        async def generate(self, req, model):
            from lira.llm.base import Block, ModelResponse, Usage

            return ModelResponse(
                [
                    Block(
                        "tool_use",
                        tool_use_id="t",
                        name="record_recommendation",
                        input={
                            "recommendation": "reverse",
                            "appeal_quote": "The client approved this in writing.",
                            "clause_id": "4.2",
                            "reason": "x",
                        },
                    )
                ],
                "tool_use",
                Usage(1, 1),
                model,
                "simulated",
            )

    from lira.llm.router import ModelRouter

    g = build_appeal_graph(AppealDeps(ctx.nlp, ModelRouter({"simulated": Misquote()}), human="skip"))
    out = await g.ainvoke(appeal("The time on line 4 was necessary for the case. We do not accept the reduction."))
    assert out["recommendation"] is None and "quoted sentence is not in the appeal" in out["problems"]


async def test_compiler_matches_the_answer_key_and_refuses_invented_rules(ctx, built):
    answers = json.loads((built["data"] / "truth" / "guidelines.json").read_text())
    doc = ctx.docs.get("kestrel", 2)
    rep = await compile_guidelines(doc, router_with({}), "Kestrel")
    got = {(r.kind, getattr(r, "event", None)) for r in rep.ruleset.rules}
    want = {(a["kind"], a.get("event")) for a in answers["kestrel@v2"]}
    assert got == want and rep.rejected == []
    bad = await compile_guidelines(doc, router_with({"hallucinate": 1.0}), "Kestrel")
    assert any(r["problems"] == ["cited clause does not exist"] for r in bad.rejected)
    assert {(r.kind, getattr(r, "event", None)) for r in bad.ruleset.rules} == want
    v1 = await compile_guidelines(ctx.docs.get("kestrel", 1), router_with({}), "Kestrel")
    changes = {c["rule"][0]: c["change"] for c in diff(v1.ruleset, rep.ruleset)}
    assert changes == {"research_cap": "added", "travel_time": "changed"}
