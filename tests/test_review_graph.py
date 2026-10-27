"""The review workflow end to end: interrupt for a person and resume, degraded mode, injections, hallucinations."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from lira import ledes
from lira.agents.review_graph import ReviewDeps, build_review_graph
from lira.domain import Finding

from .conftest import live_invoices, router_with


def deps(ctx, router=None, human="interrupt") -> ReviewDeps:
    return ReviewDeps(ctx.ref, ctx.rules, ctx.nlp, router or router_with({}), ctx.index, human=human)


def _truth(built) -> dict:
    return {(r["invoice"], r["line_no"]): r for r in map(json.loads, (built["data"] / "truth" / "lines.jsonl").read_text().splitlines())}


async def test_review_waits_for_a_person_then_settles_with_their_decisions(ctx, built):
    f = next(p for p in live_invoices(built["data"]) if "okafor" in p.name.lower() or p.name.startswith("OKA"))
    g = build_review_graph(deps(ctx), checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": f.stem}}
    first = await g.ainvoke({"ledes_text": f.read_text()}, cfg)
    assert "__interrupt__" in first
    state = (await g.aget_state(cfg)).values
    pending = [x for x in state["findings"] if x["status"] == "proposed"]
    assert pending and state["status"] == "awaiting_review"
    auto_before = Decimal(state["total_adjustment"])
    # decide half now: the run settles what is decided and waits again for the rest
    half = {x["id"]: {"decision": "accept", "by": "test"} for x in pending[: len(pending) // 2 or 1]}
    await g.ainvoke(Command(resume=half), cfg)
    mid = (await g.aget_state(cfg)).values
    rest = [x for x in mid["findings"] if x["status"] == "proposed"]
    assert len(rest) < len(pending)
    await g.ainvoke(Command(resume={x["id"]: {"decision": "reject", "by": "test"} for x in rest}), cfg)
    final = (await g.aget_state(cfg)).values
    assert final["status"] == "settled"
    assert Decimal(final["total_adjustment"]) <= auto_before
    statuses = {x["status"] for x in final["findings"]}
    assert "proposed" not in statuses and {"accepted"} <= statuses
    for x in final["findings"]:
        if x["status"] in ("auto_applied", "accepted") and x["line_no"] and x["action"] != "flag":
            assert x["explanation"].startswith(f"Line {x['line_no']}")


async def test_with_the_model_down_every_invoice_is_still_reviewed_and_nothing_judgemental_is_applied(ctx, built):
    g = build_review_graph(deps(ctx, router_with({"outage": 1.0}), human="skip"))
    for f in live_invoices(built["data"])[:6]:
        state = await g.ainvoke({"ledes_text": f.read_text()})
        assert state["status"] in ("awaiting_review", "settled")
        findings = [Finding.model_validate(x) for x in state["findings"]]
        assert all(x.source != "model" for x in findings)
        for x in findings:
            if x.kind.value in ("block_billing", "vague_description", "clerical_work", "internal_conference"):
                assert x.status == "proposed" and x.decided_by == "person"
        if state.get("batches"):
            assert state["degraded"]


async def test_entries_with_instructions_are_flagged_and_never_decided_by_the_model(ctx, built):
    truth = _truth(built)
    g = build_review_graph(deps(ctx, router_with({"obey_injection": 1.0}), human="skip"))
    seen = 0
    for f in sorted((built["data"] / "world" / "invoices").glob("*/*/*.ledes")):
        inv = ledes.parse(f.read_text())
        lines = [li.line_no for li in inv.lines if any(k["kind"] == "prompt_injection" for k in truth[(inv.number, li.line_no)]["kinds"])]
        if not lines:
            continue
        state = await g.ainvoke({"ledes_text": f.read_text()})
        for ln in lines:
            mine = [x for x in state["findings"] if x["line_no"] == ln]
            assert any(x["kind"] == "prompt_injection" for x in mine)
            assert not any(x["source"] == "model" for x in mine)
        seen += 1
        if seen >= 4:
            break
    assert seen >= 1


async def test_invented_clauses_and_quotes_never_reach_a_reviewer(ctx, built):
    g = build_review_graph(deps(ctx, router_with({"hallucinate": 0.5, "fabricate_quote": 0.5}), human="skip"))
    dropped = 0
    for f in live_invoices(built["data"])[:10]:
        state = await g.ainvoke({"ledes_text": f.read_text()})
        dropped += len(state.get("dropped", []))
        for x in state["findings"]:
            for e in x["evidence"]:
                assert e["text"] != "billed for unnecessary work"
            if x["source"] == "model":
                assert x["clause"]["clause_id"] != "12.4"
    assert dropped > 0


async def test_a_malformed_file_goes_back_to_the_firm_without_a_review(ctx):
    g = build_review_graph(deps(ctx, human="skip"))
    state = await g.ainvoke({"ledes_text": "LEDES1998B[]\nnot a header[]\n"})
    assert state["status"] == "rejected_format" and state["fatal"]


@pytest.mark.parametrize("faults", [{"invalid": 1.0}])
async def test_invalid_model_output_is_repaired_or_degrades(ctx, built, faults):
    g = build_review_graph(deps(ctx, router_with(faults), human="skip"))
    f = live_invoices(built["data"])[0]
    state = await g.ainvoke({"ledes_text": f.read_text()})
    assert state["status"] in ("awaiting_review", "settled")
