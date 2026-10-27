"""The API over PostgreSQL: row-level security, append-only decisions, roles, and the reviewer, firm and legal-ops flows.

Needs a database the test may wipe: LIRA_TEST_DATABASE_URL=postgresql://lira:lira@localhost:5432/lira_test
"""

from __future__ import annotations

import os

import httpx
import psycopg
import pytest

pytestmark = pytest.mark.db
URL = os.environ.get("LIRA_TEST_DATABASE_URL")
if not URL:
    pytest.skip("LIRA_TEST_DATABASE_URL not set", allow_module_level=True)


@pytest.fixture(scope="module")
async def app(ctx, built):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from lira.service import db
    from lira.service.app import create_app
    from lira.service.auth import create_key
    from lira.service.core import Services

    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
    pool = await db.open_pool(URL)
    await db.migrate(pool)
    cp = await db.open_pool(URL, max_size=3, extra={"autocommit": True, "prepare_threshold": 0})
    saver = AsyncPostgresSaver(cp)  # type: ignore[arg-type]
    await saver.setup()
    svc = Services(pool, ctx.ref, ctx.nlp, ctx.router, ctx.index, saver)
    await svc.seed_reference()
    for t in ctx.ref.tenants:
        await create_key(pool, t, "reviewer", f"Reviewer {t}", key=f"r-{t}")
        await create_key(pool, t, "legal_ops", f"Legal ops {t}", key=f"l-{t}")
        for f in {m.firm_id for m in ctx.ref.matters.values() if m.tenant == t}:
            await create_key(pool, t, "firm", f, firm=f, key=f"f-{f}-{t}")
        for v in ctx.docs.versions(t):
            d = ctx.docs.get(t, v)
            await svc.upload_guidelines(t, v, d.effective_from, d.text, "seed")
            await svc.approve_guidelines(t, v, "seed")
    os.environ["LIRA_WORKER"] = "external"
    application = create_app(svc)
    async with application.router.lifespan_context(application):
        yield application, svc
    await pool.close()
    await cp.close()


def client(application, key: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test", headers={"authorization": f"Bearer {key}"} if key else {}
    )


def first_invoice(built, tenant: str = "kestrel", month: str = "2026-09"):
    return sorted((built["data"] / "world" / "invoices" / tenant / month).glob("*.ledes"))[0]


async def test_row_level_security_hides_other_tenants_and_rejects_cross_tenant_writes(app):
    _, svc = app
    from lira.service.db import tenant_tx

    async with tenant_tx(svc.pool, "kestrel") as conn:
        await conn.execute("INSERT INTO audit(tenant, actor, action, object) VALUES ('kestrel', 't', 'x', 'y')")
    async with tenant_tx(svc.pool, "corvane") as conn:
        assert (await (await conn.execute("SELECT count(*) AS n FROM audit WHERE action='x'")).fetchone())["n"] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("INSERT INTO audit(tenant, actor, action, object) VALUES ('kestrel', 't', 'x', 'y')")
    async with svc.pool.connection() as conn:  # no tenant set: nothing is visible
        assert (await (await conn.execute("SELECT count(*) AS n FROM audit")).fetchone())["n"] == 0


async def test_authentication_and_roles(app, built):
    application, _ = app
    async with client(application) as c:
        assert (await c.get("/v1/invoices")).status_code == 401
    async with client(application, "nope") as c:
        assert (await c.get("/v1/invoices")).status_code == 401
    async with client(application, "r-kestrel") as c:
        assert (await c.post("/v1/guidelines/1/approve")).status_code == 403
        assert (await c.get("/healthz")).json() == {"status": "ok"}


async def test_firm_submits_reviewer_decides_and_the_invoice_settles(app, built):
    application, svc = app
    f = first_invoice(built)
    from lira import ledes

    inv = ledes.parse(f.read_text())
    async with client(application, f"f-{inv.law_firm_id}-kestrel") as firm:
        r = await firm.post("/v1/invoices", content=f.read_text())
        assert r.status_code == 202 and r.json() == {"invoice": inv.number, "status": "queued"}
        assert (await firm.post("/v1/invoices", content=f.read_text())).json()["duplicate"] is True
        bad = await firm.post("/v1/invoices", content="LEDES1998B[]\nbroken[]\n")
        assert bad.status_code == 422 and bad.json()["status"] == "rejected_format"
    assert await svc.run_jobs() >= 1
    async with client(application, "r-kestrel") as rev:
        detail = (await rev.get(f"/v1/invoices/{inv.number}")).json()
        assert detail["invoice"]["status"] == "awaiting_review"
        pending = [x for x in detail["findings"] if x["status"] == "proposed"]
        assert pending
        r = await rev.post(f"/v1/invoices/{inv.number}/decisions", json={"decisions": {x["id"]: {"decision": "accept"} for x in pending}})
        assert r.status_code == 200 and r.json()["status"] == "settled"
        unknown = await rev.post(f"/v1/invoices/{inv.number}/decisions", json={"decisions": {"nope": {"decision": "accept"}}})
        assert unknown.status_code == 404
    async with client(application, "r-corvane") as other:
        assert (await other.get(f"/v1/invoices/{inv.number}")).status_code == 404
    from lira.service.db import tenant_tx

    async with tenant_tx(svc.pool, "kestrel") as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            await conn.execute("UPDATE decisions SET decision='reject'")


async def test_firm_sees_outcomes_not_evidence_and_an_appeal_reversal_resettles(app, built):
    application, svc = app
    from lira import ledes

    f = next(p for p in sorted((built["data"] / "world" / "invoices" / "kestrel" / "2026-10").glob("*.ledes")))
    inv = ledes.parse(f.read_text())
    await svc.receive("kestrel", f.read_text(), "test")
    await svc.run_jobs()
    async with client(application, "r-kestrel") as rev:
        detail = (await rev.get(f"/v1/invoices/{inv.number}")).json()
        pending = [x for x in detail["findings"] if x["status"] == "proposed"]
        await rev.post(f"/v1/invoices/{inv.number}/decisions", json={"decisions": {x["id"]: {"decision": "accept"} for x in pending}})
        before = (await rev.get(f"/v1/invoices/{inv.number}")).json()
    applied = [x for x in before["findings"] if x["status"] in ("auto_applied", "accepted") and x["line_no"] and float(x["adjustment"]) < 0]
    assert applied
    target = applied[0]
    async with client(application, f"f-{inv.law_firm_id}-kestrel") as firm:
        seen = (await firm.get(f"/v1/invoices/{inv.number}")).json()
        assert all(set(x) <= {"id", "line_no", "kind", "action", "adjustment", "status", "explanation", "clause"} for x in seen["findings"])
        r = await firm.post(
            f"/v1/invoices/{inv.number}/appeals",
            json={"finding_id": target["id"], "message": "This was a single task, one continuous piece of work."},
        )
        assert r.status_code == 201
        appeal_id = r.json()["id"]
    async with client(application, f"f-{'okafor' if inv.law_firm_id != 'okafor' else 'harlow'}-kestrel") as other_firm:
        assert (await other_firm.get(f"/v1/invoices/{inv.number}")).status_code == 404
    async with client(application, "r-kestrel") as rev:
        r = await rev.post(f"/v1/appeals/{appeal_id}/decision", json={"decision": "reverse"})
        assert r.json()["outcome"] == "reversed"
        after = (await rev.get(f"/v1/invoices/{inv.number}")).json()
    reversed_ = next(x for x in after["findings"] if x["id"] == target["id"])
    assert reversed_["status"] == "rejected"
    assert float(after["invoice"]["total_adjustment"]) > float(before["invoice"]["total_adjustment"])


async def test_legal_ops_uploads_a_draft_that_is_not_used_until_approved(app, ctx):
    application, _svc = app
    text = ctx.docs.get("corvane", 1).text.replace("45 days", "30 days")
    async with client(application, "l-corvane") as lo:
        up = await lo.post("/v1/guidelines", json={"version": 2, "effective_from": "2026-12-01", "text": text})
        assert up.status_code == 201 and {c["rule"][0] for c in up.json()["changes"]} == {"submission_deadline"}
        versions = (await lo.get("/v1/guidelines")).json()
        assert [v["status"] for v in versions] == ["approved", "draft"]
        again = await lo.post("/v1/guidelines", json={"version": 1, "effective_from": "2025-01-01", "text": text})
        assert again.status_code == 409
        ok = await lo.post("/v1/guidelines/2/approve")
        assert ok.json()["status"] == "approved"
    async with client(application, "r-corvane") as rev:
        a = (
            await rev.post(
                "/v1/ask", json={"question": "How many days after the period end must invoices be submitted?", "on": "2026-12-15"}
            )
        ).json()
        assert a["verified"] is False or "30" in a["answer"]


async def test_the_service_refuses_a_role_that_bypasses_row_level_security():
    from contextlib import asynccontextmanager

    from lira.service.db import UnsafeRole, check_role

    class Result:
        async def fetchone(self):
            return {"rolname": "postgres", "rolsuper": True, "rolbypassrls": False}

    class Conn:
        async def execute(self, *_):
            return Result()

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield Conn()

    with pytest.raises(UnsafeRole):
        await check_role(Pool())  # type: ignore[arg-type]
