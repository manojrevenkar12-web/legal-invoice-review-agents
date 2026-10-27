"""The HTTP API (FastAPI). Run with `lira serve`; in AWS it runs on ECS Fargate behind an internal ALB.

Environment: LIRA_DATABASE_URL (required), LIRA_WORKER=inline|external (inline: this process also runs queued
reviews), LIRA_MODEL=simulated|live, LIRA_DATA_DIR / LIRA_MODELS_DIR (reference data and models).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from lira import config
from lira.context import default_embedder
from lira.llm.factory import make_router
from lira.nlp.models import NlpModels
from lira.reference import Reference
from lira.retrieval.index import HybridIndex
from lira.retrieval.qa import ask
from lira.service import db
from lira.service.auth import Principal, principal, require
from lira.service.core import Conflict, NotFound, Services


class DecisionIn(BaseModel):
    decision: Literal["accept", "reject"]
    note: str | None = Field(default=None, max_length=1000)


class DecisionsIn(BaseModel):
    decisions: dict[str, DecisionIn]


class GuidelinesIn(BaseModel):
    version: int = Field(ge=1)
    effective_from: date
    text: str = Field(min_length=50, max_length=400_000)


class AskIn(BaseModel):
    question: str = Field(min_length=5, max_length=500)
    on: date | None = None


class AppealIn(BaseModel):
    finding_id: str
    message: str = Field(min_length=5, max_length=5000)


class AppealDecisionIn(BaseModel):
    decision: Literal["uphold", "reverse"]


async def _worker(svc: Services, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            n = await svc.run_jobs()
        except Exception:  # keep the loop alive; the job row carries the error
            n = 0
        if n == 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=1.0)


async def build_services() -> tuple[Services, Any]:
    """Pools (application and LangGraph checkpoints), migrations, models, router and index for one process."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    url = db.database_url()
    pool = await db.open_pool(url)
    await db.migrate(pool)
    cp_pool = await db.open_pool(url, max_size=5, extra={"autocommit": True, "prepare_threshold": 0})
    saver = AsyncPostgresSaver(cp_pool)
    await saver.setup()
    svc = Services(
        pool,
        Reference.load(config.data_dir() / "world"),
        NlpModels.load(config.models_dir() / "nlp"),
        make_router(),
        HybridIndex(default_embedder()),
        saver,
    )
    await svc.seed_reference()
    return svc, cp_pool


def create_app(services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task = None
        cp_pool = None
        if services is None:
            svc, cp_pool = await build_services()
        else:
            svc = services
        app.state.svc = svc
        app.state.pool = svc.pool
        for t in svc.ref.tenants:
            await svc.refresh_index(t)
        if os.environ.get("LIRA_WORKER", "inline") == "inline":
            task = asyncio.create_task(_worker(svc, stop))
        yield
        stop.set()
        if task:
            await task
        if services is None:
            await svc.pool.close()
            if cp_pool:
                await cp_pool.close()

    app = FastAPI(title="Legal invoice review", version="0.1.0", lifespan=lifespan)
    origins = [o for o in os.environ.get("LIRA_CORS_ORIGINS", "").split(",") if o]
    if origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["authorization", "content-type"]
        )

    @app.exception_handler(NotFound)
    async def _nf(_: Request, e: NotFound) -> JSONResponse:
        return JSONResponse({"detail": f"not found: {e}"}, status_code=404)

    @app.exception_handler(Conflict)
    async def _cf(_: Request, e: Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(PermissionError)
    async def _pe(_: Request, e: PermissionError) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=403)

    def svc(request: Request) -> Services:
        return request.app.state.svc

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(s: Services = Depends(svc)) -> dict[str, Any]:
        async with db.system_tx(s.pool) as conn:
            await conn.execute("SELECT 1")
        return {"status": "ready", "nlp": s.nlp.version, "indexed_documents": len(s.indexed)}

    @app.get("/v1/me")
    async def me(p: Principal = Depends(principal), s: Services = Depends(svc)) -> dict[str, Any]:
        t = s.ref.tenants[p.tenant]
        return {"tenant": p.tenant, "tenant_name": t.name, "currency": t.currency, "role": p.role, "name": p.name, "firm": p.firm}

    # ---- invoices
    @app.post("/v1/invoices", status_code=202)
    async def submit(request: Request, p: Principal = Depends(require("firm", "reviewer")), s: Services = Depends(svc)) -> dict[str, Any]:
        body = (await request.body()).decode("utf-8", errors="replace")
        if len(body) > 5_000_000:
            raise HTTPException(413, "LEDES file too large")
        out = await s.receive(p.tenant, body, p.name, firm=p.firm if p.role == "firm" else None)
        if out["status"] == "rejected_format":
            return JSONResponse(out, status_code=422)  # type: ignore[return-value]
        return out

    @app.get("/v1/invoices")
    async def list_invoices(
        status: str | None = None, p: Principal = Depends(principal), s: Services = Depends(svc)
    ) -> list[dict[str, Any]]:
        rows = await s.invoices(p.tenant, status, firm=p.firm if p.role == "firm" else None)
        out = []
        for r in rows:
            r = json.loads(json.dumps(r, default=str))
            r["firm_name"] = s.ref.firms[r["firm"]].name if r["firm"] in s.ref.firms else r["firm"]
            r["matter_name"] = s.ref.matters[r["matter"]].name if r["matter"] in s.ref.matters else r["matter"]
            out.append(r)
        return out

    @app.get("/v1/invoices/{number}")
    async def get_invoice(number: str, p: Principal = Depends(principal), s: Services = Depends(svc)) -> dict[str, Any]:
        d = await s.invoice(p.tenant, number, firm=p.firm if p.role == "firm" else None)
        return json.loads(json.dumps(d, default=str))

    @app.post("/v1/invoices/{number}/review")
    async def rereview(number: str, p: Principal = Depends(require("reviewer")), s: Services = Depends(svc)) -> dict[str, Any]:
        return await s.review(p.tenant, number)

    @app.post("/v1/invoices/{number}/decisions")
    async def decide(
        number: str, body: DecisionsIn, p: Principal = Depends(require("reviewer")), s: Services = Depends(svc)
    ) -> dict[str, Any]:
        return await s.decide(p.tenant, number, {k: v.model_dump() for k, v in body.decisions.items()}, p.name)

    # ---- guidelines
    @app.get("/v1/guidelines")
    async def guidelines(p: Principal = Depends(require("reviewer", "legal_ops")), s: Services = Depends(svc)) -> list[dict[str, Any]]:
        async with db.tenant_tx(s.pool, p.tenant) as conn:
            rows = await (
                await conn.execute(
                    "SELECT version, status, effective_from, approved_by, approved_at, payload, compile_report FROM rulesets ORDER BY version"
                )
            ).fetchall()
        return [json.loads(json.dumps(r, default=str)) for r in rows]

    @app.post("/v1/guidelines", status_code=201)
    async def upload(body: GuidelinesIn, p: Principal = Depends(require("legal_ops")), s: Services = Depends(svc)) -> dict[str, Any]:
        return await s.upload_guidelines(p.tenant, body.version, body.effective_from, body.text, p.name)

    @app.post("/v1/guidelines/{version}/approve")
    async def approve(version: int, p: Principal = Depends(require("legal_ops")), s: Services = Depends(svc)) -> dict[str, Any]:
        rs = await s.approve_guidelines(p.tenant, version, p.name)
        return {"version": rs.version, "status": "approved", "rules": len(rs.rules)}

    @app.post("/v1/ask")
    async def ask_route(body: AskIn, p: Principal = Depends(principal), s: Services = Depends(svc)) -> dict[str, Any]:
        r = await ask(s.index, s.router, p.tenant, s.ref.tenants[p.tenant].name, body.question, body.on)
        return r.__dict__

    # ---- appeals
    @app.post("/v1/invoices/{number}/appeals", status_code=201)
    async def appeal(number: str, body: AppealIn, p: Principal = Depends(require("firm")), s: Services = Depends(svc)) -> dict[str, Any]:
        if not p.firm:
            raise HTTPException(403, "firm key required")
        return json.loads(json.dumps(await s.appeal(p.tenant, number, body.finding_id, body.message, p.firm), default=str))

    @app.post("/v1/appeals/{appeal_id}/decision")
    async def decide_appeal(
        appeal_id: int, body: AppealDecisionIn, p: Principal = Depends(require("reviewer")), s: Services = Depends(svc)
    ) -> dict[str, Any]:
        return await s.decide_appeal(p.tenant, appeal_id, body.decision, p.name)

    # ---- human evaluation: the pinned annotation round, blind (no source, no confidence), shuffled per annotator
    @app.get("/v1/annotation/items")
    async def annotation_items(p: Principal = Depends(require("reviewer"))) -> list[dict[str, Any]]:
        import hashlib

        path = config.repo_root() / "evals" / "annotation" / "round-1" / "items.jsonl"
        if not path.exists():
            raise HTTPException(404, "no annotation round yet: run `lira eval`")
        items = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        keep = ("id", "kind", "action", "clause_id", "clause_text", "line_no", "timekeeper_role", "hours", "description", "explanation")
        return sorted(({k: it[k] for k in keep} for it in items), key=lambda it: hashlib.sha1((p.name + it["id"]).encode()).hexdigest())

    # ---- evaluation and monitoring (generated reports)
    @app.get("/v1/evaluation")
    async def evaluation(_: Principal = Depends(require("reviewer", "legal_ops"))) -> dict[str, Any]:
        path = config.reports_dir() / "eval" / "report.json"
        if not path.exists():
            raise HTTPException(404, "no evaluation report yet: run `lira eval`")
        r = json.loads(path.read_text())
        r["golden"]["auto_applied"].pop("lines_wrong", None)
        return r

    return app
