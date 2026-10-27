"""The held-out runner: first runs are written once, later runs never overwrite them, edited sets are refused."""

from __future__ import annotations

import json

import pytest

from lira.eval.heldout import SetChanged, run_set

GUIDE = """# Example Co - Billing Guidelines

## 1. Time

1.1 Time must be recorded in increments of one-tenth of an hour (0.1).

1.2 We welcome questions about these guidelines.
"""
KEY = """client_name: Example Co
tenant: example
version: 1
effective_from: 2026-01-01
rules:
  - kind: billing_increment
    clause_id: "1.1"
    hours: 0.1
not_rules: ["1.2"]
"""
ENTRIES = """entries:
  - id: e1
    description: Draft motion to compel production of the 2024 sales ledgers
    timekeeper_class: AS
    activity_code: A103
    task_code: L350
    hours: 2.4
    labels: {}
  - id: e2
    description: Attention to file
    timekeeper_class: AS
    activity_code: A104
    hours: 1.0
    labels: {vague_description: true}
"""


async def test_first_run_is_pinned_and_never_rewritten(tmp_path, built):
    root, reports = tmp_path / "heldout", tmp_path / "reports"
    (root / "g").mkdir(parents=True)
    (root / "g" / "guidelines.md").write_text(GUIDE)
    (root / "g" / "answer_key.yaml").write_text(KEY)
    first = await run_set("g", root=root, reports=reports)
    assert first["scores"]["found"] == 1 and first["scores"]["rules_from_non_rule_clauses"] == []
    pinned = (reports / "heldout" / "g" / "first_run.json").read_text()
    await run_set("g", root=root, reports=reports)
    assert (reports / "heldout" / "g" / "first_run.json").read_text() == pinned
    assert (reports / "heldout" / "g" / "latest.json").exists()
    (root / "g" / "STATUS").write_text("regression\n")  # bookkeeping does not change the set
    await run_set("g", root=root, reports=reports)
    (root / "g" / "answer_key.yaml").write_text(KEY.replace("0.1", "0.25"))
    with pytest.raises(SetChanged):
        await run_set("g", root=root, reports=reports)


async def test_entries_go_through_the_whole_review(tmp_path, built):
    root, reports = tmp_path / "heldout", tmp_path / "reports"
    (root / "e").mkdir(parents=True)
    (root / "e" / "entries.yaml").write_text(ENTRIES)
    r = await run_set("e", root=root, reports=reports)
    assert r["entries"] == 2 and {x["id"] for x in r["rows"]} == {"e1", "e2"}
    assert json.loads((reports / "heldout" / "e" / "first_run.json").read_text())["kind"] == "entries"
    assert "vague_description" in next(x for x in r["rows"] if x["id"] == "e2")["predicted"]
