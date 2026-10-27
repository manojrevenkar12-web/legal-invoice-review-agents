"""lira: the command line.

lira run [--quick]          simulate, train, compile and approve guidelines, evaluate, gate
lira sim                    build the simulated world (data/world, data/truth)
lira train                  train the classical NLP models on review history (models/nlp)
lira guidelines compile     compile every guideline document into a draft RuleSet (reports/guidelines)
lira guidelines approve     approve a draft (legal operations), e.g. --tenant kestrel --version 2 --by "A. Weber"
lira review FILE            review one LEDES invoice (JSON out); --accept-all to settle it as if every proposal were accepted
lira ask --tenant T Q       ask the guidelines a question
lira drift                  run the drift monitor over all invoices
lira eval [--quick]         run every evaluation (reports/eval)
lira gate                   check the reports against config/release.yaml
lira prompts lock|check     pin prompt versions / check them against their regression runs
lira heldout run SET        run a blind held-out set once and record its first run
lira serve                  the API (FastAPI) on :8080
lira worker                 the review worker (SQS intake when configured, PostgreSQL job queue)
lira demo                   seed PostgreSQL with the simulated world and serve the API for the console
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from lira import config


def _json(x: Any) -> None:
    print(json.dumps(x, indent=2, default=str))


def cmd_sim(_: argparse.Namespace) -> int:
    from lira.sim.world import main

    _json(main(str(config.config_dir() / "world.yaml"), str(config.data_dir())))
    return 0


def cmd_train(_: argparse.Namespace) -> int:
    from lira.nlp.models import train

    hist = config.data_dir() / "world" / "history"
    rows = [json.loads(x) for x in (hist / "decisions.jsonl").read_text().splitlines()]
    appeals = [json.loads(x) for x in (hist / "appeals.jsonl").read_text().splitlines()] if (hist / "appeals.jsonl").exists() else None
    months = sorted({r["date"][:7] for r in rows})
    validation_from = months[-2] + "-01" if len(months) > 3 else None  # the last two history months are held out
    model = train(rows, appeals, validation_from=validation_from)
    model.save(config.models_dir() / "nlp")
    out = config.reports_dir() / "models"
    out.mkdir(parents=True, exist_ok=True)
    (out / "nlp_model_card.json").write_text(json.dumps(model.card, indent=2, default=str))
    _json(model.card)
    return 0


async def _compile(approve_as: str | None, tenant: str | None) -> int:
    from lira.guidelines.compiler import compile_guidelines
    from lira.guidelines.store import DocumentStore, RuleStore
    from lira.llm.factory import make_router
    from lira.reference import Reference

    data = config.data_dir()
    docs = DocumentStore(data / "world" / "guidelines")
    rules = RuleStore(data / "rulesets")
    ref = Reference.load(data / "world")
    router = make_router()
    out = config.reports_dir() / "guidelines"
    out.mkdir(parents=True, exist_ok=True)
    for t in docs.tenants():
        if tenant and t != tenant:
            continue
        prev = None
        for v in docs.versions(t):
            rep = await compile_guidelines(docs.get(t, v), router, ref.tenants[t].name, prev)
            existing = rules.get(t, v)
            if existing is None or existing.status != "approved" or existing.document_sha256 != rep.ruleset.document_sha256:
                rules.save(rep.ruleset)
            (out / f"{t}-v{v}.json").write_text(
                json.dumps(
                    {
                        "rules": [r.model_dump(mode="json") for r in rep.ruleset.rules],
                        "rejected": rep.rejected,
                        "disagreements_with_patterns": rep.disagreements,
                        "changes_from_previous_version": rep.changes,
                        "model": rep.model,
                    },
                    indent=2,
                    default=str,
                )
            )
            if approve_as:
                rules.approve(t, v, approve_as)
            print(
                f"{t} v{v}: {len(rep.ruleset.rules)} rules, {len(rep.rejected)} rejected by the verifier, "
                f"{len(rep.disagreements)} differences from the pattern extractor, {len(rep.changes)} changes from v{v - 1 if v > 1 else '-'}"
            )
            prev = rules.get(t, v)
    return 0


def cmd_guidelines(a: argparse.Namespace) -> int:
    if a.action == "compile":
        return asyncio.run(_compile(a.approve_as, a.tenant))
    from lira.guidelines.store import RuleStore

    rs = RuleStore(config.data_dir() / "rulesets").approve(a.tenant, a.version, a.by)
    print(f"approved {rs.tenant} v{rs.version} by {rs.approved_by}")
    return 0


async def _review(path: Path, accept_all: bool) -> dict[str, Any]:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from lira.agents.review_graph import ReviewDeps, build_review_graph
    from lira.context import Context

    ctx = Context.load()
    g = build_review_graph(ReviewDeps(ctx.ref, ctx.rules, ctx.nlp, ctx.router, ctx.index), checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": path.stem}}
    text = await asyncio.to_thread(path.read_text)
    state = await g.ainvoke({"ledes_text": text}, cfg)
    if accept_all and "__interrupt__" in state:
        pending = state["__interrupt__"][0].value["pending"]
        await g.ainvoke(Command(resume={fid: {"decision": "accept", "by": "cli"} for fid in pending}), cfg)
    state = dict((await g.aget_state(cfg)).values)
    return {
        k: v for k, v in state.items() if k in ("status", "fatal", "findings", "adjustments", "total_adjustment", "degraded", "dropped")
    }


def cmd_review(a: argparse.Namespace) -> int:
    _json(asyncio.run(_review(Path(a.file), a.accept_all)))
    return 0


def cmd_ask(a: argparse.Namespace) -> int:
    from lira.context import Context
    from lira.retrieval.qa import ask

    ctx = Context.load()
    r = asyncio.run(
        ask(ctx.index, ctx.router, a.tenant, ctx.ref.tenants[a.tenant].name, a.question, date.fromisoformat(a.on) if a.on else None)
    )
    _json(r.__dict__)
    return 0


def cmd_drift(_: argparse.Namespace) -> int:
    from lira import ledes
    from lira.monitor.drift import monitor
    from lira.nlp.models import NlpModels

    rows: list[dict[str, Any]] = []
    for p in sorted((config.data_dir() / "world" / "invoices").glob("*/*/*.ledes")):
        inv = ledes.parse(p.read_text())
        rows += [
            {
                "firm": inv.law_firm_id,
                "month": f"{li.date:%Y-%m}",
                "description": li.description,
                "activity_code": li.activity_code,
                "timekeeper_class": li.timekeeper_class,
                "units": float(li.units),
            }
            for li in inv.lines
            if li.is_fee
        ]
    months: list[str] = sorted({str(r["month"]) for r in rows})
    world = json.loads((config.data_dir() / "world" / "world.json").read_text())
    h = int(world["history_months"])
    _json(monitor(rows, NlpModels.load(config.models_dir() / "nlp"), (months[h - 2], months[h - 1]))["alerts"])
    return 0


def cmd_eval(a: argparse.Namespace) -> int:
    from lira.eval.run import run_all

    r = asyncio.run(run_all(quick=a.quick))
    print((config.reports_dir() / "eval" / "SUMMARY.md").read_text())
    print(f"({r['seconds']} s)")
    return 0


def cmd_gate(_: argparse.Namespace) -> int:
    from lira.eval.gate import main

    return main()


def cmd_prompts(a: argparse.Namespace) -> int:
    from lira.llm.prompts import check_lock, lock_entries

    lock = config.repo_root() / "prompts.lock"
    if a.action == "lock":
        lock.write_text(json.dumps(lock_entries(), indent=2) + "\n")
        print(f"wrote {lock}")
        return 0
    problems = check_lock(lock, config.reports_dir() / "prompts" / "history.jsonl")
    for p in problems:
        print("FAIL", p)
    print("prompts: clean" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_heldout(a: argparse.Namespace) -> int:
    from lira.eval.heldout import run_set

    _json(asyncio.run(run_set(a.set, force=False)))
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("lira.service.app:create_app", factory=True, host=a.host, port=a.port)
    return 0


def cmd_demo(a: argparse.Namespace) -> int:
    """Seed the database with the simulated world, then serve the API for the console."""
    import os

    import uvicorn

    async def _seed() -> dict[str, Any]:
        from lira.service.app import build_services
        from lira.service.demo import seed

        svc, cp = await build_services()
        out = await seed(svc)
        await svc.pool.close()
        await cp.close()
        return out

    _json(asyncio.run(_seed()))
    if a.seed_only:
        return 0
    os.environ.setdefault("LIRA_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    uvicorn.run("lira.service.app:create_app", factory=True, host=a.host, port=a.port)
    return 0


def cmd_worker(_: argparse.Namespace) -> int:
    """Review worker: consume the SQS intake queue when configured, and run queued jobs from PostgreSQL."""
    import os

    async def _loop() -> None:
        from lira.service.app import build_services

        svc, cp = await build_services()
        worker = None
        if os.environ.get("LIRA_REVIEW_QUEUE_URL"):
            import boto3

            from lira.aws.ingest import SqsWorker

            worker = SqsWorker(
                boto3.client("sqs"),
                boto3.client("s3"),
                os.environ["LIRA_REVIEW_QUEUE_URL"],
                lambda t, text, actor: svc.receive(t, text, actor),
                svc.run_jobs,
            )
        try:
            while True:
                n = await worker.poll_once() if worker else 0
                n += await svc.run_jobs()
                if not n and not worker:
                    await asyncio.sleep(1.0)
        finally:
            await svc.pool.close()
            await cp.close()

    asyncio.run(_loop())
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    for step in (cmd_sim, cmd_train):
        if step(a):
            return 1
    if asyncio.run(_compile("legal-ops (simulation)", None)):
        return 1
    if cmd_eval(a):
        return 1
    return cmd_gate(a)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lira", description="Legal invoice review agents")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sim").set_defaults(fn=cmd_sim)
    sub.add_parser("train").set_defaults(fn=cmd_train)
    g = sub.add_parser("guidelines")
    g.add_argument("action", choices=["compile", "approve"])
    g.add_argument("--tenant")
    g.add_argument("--version", type=int)
    g.add_argument("--by", default="legal-ops")
    g.add_argument("--approve-as", help="simulation only: approve every compiled draft as this person")
    g.set_defaults(fn=cmd_guidelines)
    r = sub.add_parser("review")
    r.add_argument("file")
    r.add_argument("--accept-all", action="store_true")
    r.set_defaults(fn=cmd_review)
    q = sub.add_parser("ask")
    q.add_argument("--tenant", required=True)
    q.add_argument("--on")
    q.add_argument("question")
    q.set_defaults(fn=cmd_ask)
    sub.add_parser("drift").set_defaults(fn=cmd_drift)
    e = sub.add_parser("eval")
    e.add_argument("--quick", action="store_true")
    e.set_defaults(fn=cmd_eval)
    sub.add_parser("gate").set_defaults(fn=cmd_gate)
    pr = sub.add_parser("prompts")
    pr.add_argument("action", choices=["lock", "check"])
    pr.set_defaults(fn=cmd_prompts)
    h = sub.add_parser("heldout")
    h.add_argument("action", choices=["run"])
    h.add_argument("set")
    h.set_defaults(fn=cmd_heldout)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("worker").set_defaults(fn=cmd_worker)
    d = sub.add_parser("demo")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8080)
    d.add_argument("--seed-only", action="store_true")
    d.set_defaults(fn=cmd_demo)
    rn = sub.add_parser("run")
    rn.add_argument("--quick", action="store_true")
    rn.set_defaults(fn=cmd_run)
    args = p.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
