"""Shared fixtures. The simulated world, models and approved rules are built once per test session in a temp dir."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("LIRA_ROOT", str(ROOT))


@pytest.fixture(scope="session")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """World, truth, trained NLP models and approved RuleSets, as `lira run` produces them."""
    from lira.guidelines.compiler import compile_guidelines
    from lira.guidelines.store import DocumentStore, RuleStore
    from lira.llm.router import ModelRouter
    from lira.llm.simulated import SimulatedProvider
    from lira.nlp.models import train
    from lira.reference import Reference
    from lira.sim.world import World

    base = tmp_path_factory.mktemp("lira")
    data, models = base / "data", base / "models"
    World(ROOT / "config" / "world.yaml", data).build()
    hist = data / "world" / "history"
    rows = [json.loads(x) for x in (hist / "decisions.jsonl").read_text().splitlines()]
    appeals = [json.loads(x) for x in (hist / "appeals.jsonl").read_text().splitlines()]
    train(rows, appeals, validation_from="2026-06-01").save(models / "nlp")
    docs, rules, ref = DocumentStore(data / "world" / "guidelines"), RuleStore(data / "rulesets"), Reference.load(data / "world")
    router = ModelRouter({"simulated": SimulatedProvider({})})

    async def compile_all() -> None:
        for t in docs.tenants():
            for v in docs.versions(t):
                rep = await compile_guidelines(docs.get(t, v), router, ref.tenants[t].name)
                rules.save(rep.ruleset)
                rules.approve(t, v, "legal-ops (test)")

    asyncio.run(compile_all())
    return {"data": data, "models": models}


@pytest.fixture(scope="session")
def ctx(built: dict[str, Path]):
    from lira.context import Context
    from lira.llm.router import ModelRouter
    from lira.llm.simulated import SimulatedProvider
    from lira.retrieval.index import HashingEmbedder

    return Context.load(
        data=built["data"], models=built["models"], router=ModelRouter({"simulated": SimulatedProvider({})}), embedder=HashingEmbedder()
    )


def router_with(faults: dict[str, float]):
    from lira.llm.router import ModelRouter
    from lira.llm.simulated import SimulatedProvider

    async def nosleep(_s: float) -> None:
        return None

    return ModelRouter({"simulated": SimulatedProvider(faults)}, sleep=nosleep)


def live_invoices(data: Path, month: str = "2026-09") -> list[Path]:
    return sorted((data / "world" / "invoices").glob(f"*/{month}/*.ledes"))
