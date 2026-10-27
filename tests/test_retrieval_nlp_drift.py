"""Retrieval never crosses tenants and answers are checked; classical NLP pieces; drift statistics."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from lira.guidelines.documents import parse
from lira.llm.schemas import Answer
from lira.monitor.drift import psi_categorical, psi_numeric
from lira.nlp.entities import Roster, mentions, pseudonymize
from lira.nlp.text import normalize, segments, task_segments
from lira.retrieval.index import HashingEmbedder, Hit, HybridIndex, chunk, retrieve
from lira.retrieval.qa import ask, check_answer

from .conftest import router_with

DOC_A = "# A\n\n## 3. Time\n\n3.1 Time must be recorded in increments of one-tenth of an hour (0.1).\n\n## 5. Travel\n\n5.1 Travel time is not billable.\n"
DOC_B = "# B\n\n## 2. Travel\n\n2.1 Travel time may be billed at 50% of the approved rate.\n"


def index() -> HybridIndex:
    ix = HybridIndex(HashingEmbedder())
    ix.add(chunk(parse(DOC_A, "acme", 1, date(2025, 1, 1))))
    ix.add(chunk(parse(DOC_A.replace("not billable", "billed at 25% of the rate"), "acme", 2, date(2026, 5, 1))))
    ix.add(chunk(parse(DOC_B, "beta", 1, date(2025, 1, 1))))
    return ix


def test_search_requires_a_tenant_and_never_returns_another_tenants_clauses():
    ix = index()
    with pytest.raises(ValueError):
        ix.search("travel", "")
    hits = retrieve(ix, "Is travel time billable?", "acme", k=5)
    assert hits and all(h.chunk.tenant == "acme" for h in hits)
    assert all(h.chunk.tenant == "beta" for h in retrieve(ix, "travel", "beta"))


def test_search_as_of_a_date_uses_the_version_in_force():
    ix = index()
    old = retrieve(ix, "travel time", "acme", on=date(2026, 1, 15))
    new = retrieve(ix, "travel time", "acme", on=date(2026, 9, 1))
    assert {h.chunk.version for h in old} == {1} and {h.chunk.version for h in new} == {2}


def test_answers_are_shown_only_if_citations_and_numbers_check_out():
    ix = index()
    hits = retrieve(ix, "travel", "acme", on=date(2026, 9, 1))
    good = Answer(answer="Travel is billed at 25% of the rate [5.1].", citations=["5.1"], answerable=True)
    assert check_answer(good, hits) == []
    assert check_answer(Answer(answer="Travel is billed at 50% [5.1].", citations=["5.1"], answerable=True), hits) == [
        "number 50 is not in the cited clauses"
    ]
    assert "cites clause 9.9, which was not retrieved" in check_answer(Answer(answer="x", citations=["9.9"], answerable=True), hits)
    assert check_answer(Answer(answer="x", citations=[], answerable=True), [Hit(hits[0].chunk, 1.0)]) == ["answer without citations"]


async def test_hallucinated_citations_become_an_abstention():
    ix = index()
    r = await ask(ix, router_with({"hallucinate": 1.0}), "acme", "Acme", "Is travel time billable?", on=date(2026, 9, 1))
    assert not r.verified and r.citations == [] and "could not find a supported answer" in r.answer
    r2 = await ask(ix, router_with({}), "acme", "Acme", "What is the policy on contingency fee arrangements?", on=date(2026, 9, 1))
    assert not r2.answerable


def test_segmentation_tells_one_task_from_several():
    assert len(task_segments("Review and analyze responses to interrogatories")) == 1
    assert len(task_segments("Prepare for and attend deposition of J. Brandt")) == 1
    assert len(task_segments("Draft motion to compel; TC w/ client re: settlement; rev production")) == 3
    assert len(task_segments("Draft answer to the complaint, review the claimant's expert report and prepare chronology")) == 3
    assert normalize("L330/A104 REV DEPO TRANSCRIPT") == "review deposition transcript"
    assert segments("Copy and distribute pleadings to the team") == ["copy and distribute pleadings to the team"]


def test_names_resolve_to_the_firm_roster_and_are_masked_before_a_model_sees_them():
    roster = Roster([("T1", "Mira Okafor"), ("T2", "Jonas Lindqvist")])
    text = "Conference with M. Okafor and Jonas Brandt regarding Bates label issue"
    ms = mentions(text, roster)
    found = {m.text: m.timekeeper_id for m in ms if m.label == "PERSON"}
    assert found.get("M. Okafor") == "T1"
    assert found.get("Jonas Brandt") is None  # a witness, not at the firm
    masked, names = pseudonymize(text, ms)
    assert "Okafor" not in masked and "Jonas Brandt" not in masked and "Bates label" in masked
    assert len(set(names.values())) == 2
    assert all(m.text != "TC" for m in mentions("TC W/ CLIENT RE DEPO", Roster([("T3", "Tara Cole")])))


def test_psi_is_zero_for_the_same_distribution_and_large_for_a_shift():
    rng = np.random.default_rng(1)
    a, b = rng.normal(0, 1, 2000), rng.normal(0, 1, 2000)
    assert psi_numeric(a, b, 10) < 0.05
    assert psi_numeric(a, rng.normal(1.5, 1, 2000), 10) > 0.5
    assert psi_categorical(["x"] * 50 + ["y"] * 50, ["x"] * 50 + ["y"] * 50) < 1e-6
    assert psi_categorical(["x"] * 90 + ["y"] * 10, ["x"] * 10 + ["y"] * 90) > 1.0


def test_classical_models_score_lines_and_flag_unusual_hours(ctx):
    rows = [
        {"description": "Attention to file", "timekeeper_class": "AS", "activity_code": "A104", "units": 1.0},
        {
            "description": "Draft motion to compel; review production; call with client re settlement",
            "timekeeper_class": "AS",
            "activity_code": "A103",
            "units": 4.0,
        },
        {"description": "Bates label production documents", "timekeeper_class": "LA", "activity_code": "A110", "units": 1.0},
    ]
    p = ctx.nlp.judgement_probs(rows)
    assert p["vague_description"][0] > p["vague_description"][1]
    assert p["block_billing"][1] > p["block_billing"][0]
    assert p["clerical_work"][2] > p["clerical_work"][1]
    codes, conf = ctx.nlp.task_codes(["Attend deposition of Jonas Brandt"])
    assert codes[0] == "L330" and conf[0] > 0.3
    assert ctx.nlp.hours_z("L250", "A103", "AS", 30.0) > ctx.nlp.hours_z("L250", "A103", "AS", 1.0)
    intents = ctx.nlp.intents(["We accept the adjustment on line 4.", "Which clause applies to line 7?"])
    assert [i for i, _ in intents] == ["accept", "request_info"]
