"""What the API does, over PostgreSQL: receive invoices, run reviews, record decisions, guidelines, appeals.

Reviews run as LangGraph threads checkpointed in PostgreSQL (thread id "<tenant>/<invoice>"), so a review
interrupted for a person resumes in any process, hours or days later. Decisions and the audit trail are
append-only rows; findings and invoices are projections that are rewritten after each step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from langgraph.types import Command
from psycopg.types.json import Jsonb

from lira import ledes
from lira.agents.appeal_graph import AppealDeps, build_appeal_graph
from lira.agents.review_graph import ReviewDeps, build_review_graph, settle_state
from lira.guidelines.compiler import compile_guidelines
from lira.guidelines.documents import parse as parse_doc
from lira.guidelines.rules import RuleSet
from lira.guidelines.store import MemoryRules
from lira.llm.router import ModelRouter
from lira.nlp.models import NlpModels
from lira.reference import Reference
from lira.retrieval.index import HybridIndex, chunk
from lira.service.db import Pool, system_tx, tenant_tx


class NotFound(LookupError):
    pass


class Conflict(Exception):
    pass


@dataclass
class Services:
    pool: Pool
    ref: Reference
    nlp: NlpModels
    router: ModelRouter
    index: HybridIndex
    checkpointer: Any = None
    indexed: set[tuple[str, int]] = field(default_factory=set)

    # ---------------------------------------------------------------- setup
    async def seed_reference(self) -> None:
        async with system_tx(self.pool) as conn:
            for t in self.ref.tenants.values():
                await conn.execute(
                    "INSERT INTO tenants(id, name, client_id, currency) VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                    (t.id, t.name, t.client_id, t.currency),
                )

    async def audit(self, conn: Any, tenant: str, actor: str, action: str, obj: str, detail: dict[str, Any] | None = None) -> None:
        await conn.execute(
            "INSERT INTO audit(tenant, actor, action, object, detail) VALUES (%s,%s,%s,%s,%s)",
            (tenant, actor, action, obj, Jsonb(detail or {})),
        )

    # ---------------------------------------------------------------- guidelines
    async def rulesets(self, tenant: str) -> list[RuleSet]:
        async with tenant_tx(self.pool, tenant) as conn:
            rows = await (await conn.execute("SELECT payload FROM rulesets ORDER BY version")).fetchall()
        return [RuleSet.model_validate(r["payload"]) for r in rows]

    async def refresh_index(self, tenant: str) -> None:
        async with tenant_tx(self.pool, tenant) as conn:
            rows = await (await conn.execute("SELECT version, effective_from, document FROM rulesets WHERE status = 'approved'")).fetchall()
        for r in rows:
            if (tenant, r["version"]) in self.indexed:
                continue
            self.index.add(chunk(parse_doc(r["document"], tenant, r["version"], r["effective_from"])))
            self.indexed.add((tenant, r["version"]))

    async def upload_guidelines(self, tenant: str, version: int, effective_from: date, text: str, actor: str) -> dict[str, Any]:
        doc = parse_doc(text, tenant, version, effective_from)
        existing = await self.rulesets(tenant)
        if any(r.version == version and r.status == "approved" for r in existing):
            raise Conflict(f"version {version} is approved; upload a new version")
        prev = max((r for r in existing if r.status == "approved" and r.version < version), key=lambda r: r.version, default=None)
        rep = await compile_guidelines(doc, self.router, self.ref.tenants[tenant].name, prev)
        report = {"rejected": rep.rejected, "disagreements_with_patterns": rep.disagreements, "changes": rep.changes, "model": rep.model}
        async with tenant_tx(self.pool, tenant) as conn:
            await conn.execute(
                """INSERT INTO rulesets(tenant, version, status, document_sha256, effective_from, payload, compile_report, document)
                   VALUES (%s,%s,'draft',%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant, version) DO UPDATE SET status='draft', document_sha256=EXCLUDED.document_sha256,
                   effective_from=EXCLUDED.effective_from, payload=EXCLUDED.payload, compile_report=EXCLUDED.compile_report,
                   document=EXCLUDED.document, approved_by=NULL, approved_at=NULL""",
                (tenant, version, doc.sha256, effective_from, Jsonb(json.loads(rep.ruleset.model_dump_json())), Jsonb(report), text),
            )
            await self.audit(conn, tenant, actor, "guidelines.compiled", f"v{version}", {"rules": len(rep.ruleset.rules)})
        return {"version": version, "rules": len(rep.ruleset.rules), **report}

    async def approve_guidelines(self, tenant: str, version: int, actor: str) -> RuleSet:
        async with tenant_tx(self.pool, tenant) as conn:
            row = await (await conn.execute("SELECT payload, status FROM rulesets WHERE version=%s FOR UPDATE", (version,))).fetchone()
            if row is None:
                raise NotFound(f"guidelines v{version}")
            rs = RuleSet.model_validate(row["payload"])
            if row["status"] != "approved":
                rs.status, rs.approved_by = "approved", actor
                rs.approved_at = None
                await conn.execute(
                    "UPDATE rulesets SET status='approved', approved_by=%s, approved_at=now(), payload=%s WHERE version=%s",
                    (actor, Jsonb(json.loads(rs.model_dump_json())), version),
                )
                await self.audit(conn, tenant, actor, "guidelines.approved", f"v{version}")
        await self.refresh_index(tenant)
        return rs

    # ---------------------------------------------------------------- invoices
    def _deps(self, rules: list[RuleSet], spent: Decimal) -> ReviewDeps:
        return ReviewDeps(
            self.ref,
            MemoryRules(rules),
            self.nlp,
            self.router,
            self.index,
            spend_before=lambda inv: spent,
            human="interrupt",
        )

    async def receive(self, tenant: str, text: str, actor: str, firm: str | None = None) -> dict[str, Any]:
        """Store a LEDES file and queue its review. Format problems are returned to the sender at once."""
        try:
            inv = ledes.parse(text)
        except ledes.LedesError as e:
            return {"status": "rejected_format", "problems": [i.message for i in e.issues]}
        t = self.ref.tenant_for_client(inv.client_id)
        if t is None or t.id != tenant:
            return {"status": "rejected_format", "problems": [f"CLIENT_ID {inv.client_id} is not this client"]}
        if firm and inv.law_firm_id != firm:
            raise PermissionError("a firm can only submit its own invoices")
        async with tenant_tx(self.pool, tenant) as conn:
            exists = await (await conn.execute("SELECT status FROM invoices WHERE number=%s", (inv.number,))).fetchone()
            if exists:
                return {"invoice": inv.number, "status": exists["status"], "duplicate": True}
            await conn.execute(
                """INSERT INTO invoices(tenant, number, firm, matter, period_start, period_end, total, status, ledes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'queued',%s)""",
                (tenant, inv.number, inv.law_firm_id, inv.client_matter_id, inv.period_start, inv.period_end, inv.total, text),
            )
            await self.audit(conn, tenant, actor, "invoice.received", inv.number, {"total": str(inv.total)})
        async with system_tx(self.pool) as conn:
            await conn.execute("INSERT INTO jobs(tenant, kind, payload) VALUES (%s,'review',%s)", (tenant, Jsonb({"invoice": inv.number})))
        return {"invoice": inv.number, "status": "queued"}

    async def _spent_before(self, tenant: str, matter: str, number: str, period_start: date | None) -> Decimal:
        async with tenant_tx(self.pool, tenant) as conn:
            row = await (
                await conn.execute(
                    "SELECT coalesce(sum(total), 0) AS s FROM invoices WHERE matter=%s AND number<>%s AND period_start < %s",
                    (matter, number, period_start or date.max),
                )
            ).fetchone()
        return Decimal(row["s"]) if row else Decimal("0")

    async def review(self, tenant: str, number: str) -> dict[str, Any]:
        async with tenant_tx(self.pool, tenant) as conn:
            row = await (await conn.execute("SELECT * FROM invoices WHERE number=%s", (number,))).fetchone()
        if row is None:
            raise NotFound(number)
        rules = await self.rulesets(tenant)
        await self.refresh_index(tenant)
        spent = await self._spent_before(tenant, row["matter"], number, row["period_start"])
        graph = build_review_graph(self._deps(rules, spent), checkpointer=self.checkpointer)
        cfg = {"configurable": {"thread_id": f"{tenant}/{number}"}}
        await graph.ainvoke({"ledes_text": row["ledes"]}, cfg)
        state = dict((await graph.aget_state(cfg)).values)
        await self._store(tenant, number, state)
        return self._summary(number, state)

    async def decide(self, tenant: str, number: str, decisions: dict[str, dict[str, Any]], actor: str) -> dict[str, Any]:
        async with tenant_tx(self.pool, tenant) as conn:
            row = await (await conn.execute("SELECT status FROM invoices WHERE number=%s", (number,))).fetchone()
            if row is None:
                raise NotFound(number)
            known = {r["id"] for r in await (await conn.execute("SELECT id FROM findings WHERE invoice=%s", (number,))).fetchall()}
            unknown = set(decisions) - known
            if unknown:
                raise NotFound(f"findings {sorted(unknown)[:3]}")
            for fid, d in decisions.items():
                await conn.execute(
                    "INSERT INTO decisions(tenant, invoice, finding_id, decision, by_user, note) VALUES (%s,%s,%s,%s,%s,%s)",
                    (tenant, number, fid, d["decision"], actor, d.get("note")),
                )
            await self.audit(conn, tenant, actor, "findings.decided", number, {"count": len(decisions)})
        all_decisions = await self._decisions(tenant, number)
        rules = await self.rulesets(tenant)
        graph = build_review_graph(self._deps(rules, Decimal("0")), checkpointer=self.checkpointer)
        cfg = {"configurable": {"thread_id": f"{tenant}/{number}"}}
        snap = await graph.aget_state(cfg)
        if snap.next:  # still waiting at human_review: resume the thread
            await graph.ainvoke(Command(resume=all_decisions), cfg)
            state = dict((await graph.aget_state(cfg)).values)
        else:  # already settled (e.g. an appeal reversed a finding): settle again from the stored state
            state = dict(snap.values)
            state["decisions"] = all_decisions
            state |= settle_state(self._deps(rules, Decimal(state.get("spent_before", "0"))), state)
            await graph.aupdate_state(
                cfg, {"decisions": all_decisions, **{k: state[k] for k in ("findings", "adjustments", "total_adjustment", "status")}}
            )
        await self._store(tenant, number, state)
        return self._summary(number, state)

    async def _decisions(self, tenant: str, number: str) -> dict[str, dict[str, Any]]:
        async with tenant_tx(self.pool, tenant) as conn:
            rows = await (
                await conn.execute("SELECT finding_id, decision, by_user, note, at FROM decisions WHERE invoice=%s ORDER BY id", (number,))
            ).fetchall()
        return {
            r["finding_id"]: {"decision": r["decision"], "by": r["by_user"], "note": r["note"], "at": r["at"].isoformat()} for r in rows
        }

    async def _store(self, tenant: str, number: str, state: dict[str, Any]) -> None:
        status = state.get("status", "awaiting_review")
        async with tenant_tx(self.pool, tenant) as conn:
            await conn.execute(
                """UPDATE invoices SET status=%s, total_adjustment=%s, degraded=%s, model_cost_usd=%s,
                   settled_at = CASE WHEN %s='settled' THEN now() ELSE settled_at END, state=%s WHERE number=%s""",
                (
                    status,
                    Decimal(state.get("total_adjustment") or "0"),
                    bool(state.get("degraded")),
                    float(sum(state.get("model_cost_usd", []))),
                    status,
                    Jsonb(
                        {
                            "fatal": state.get("fatal", []),
                            "adjustments": state.get("adjustments", {}),
                            "dropped": len(state.get("dropped", [])),
                        }
                    ),
                    number,
                ),
            )
            for f in state.get("findings", []):
                await conn.execute(
                    """INSERT INTO findings(tenant, invoice, id, line_no, kind, source, decided_by, action, status, adjustment, severity, confidence, payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (tenant, id) DO UPDATE SET status=EXCLUDED.status, adjustment=EXCLUDED.adjustment,
                       severity=EXCLUDED.severity, confidence=EXCLUDED.confidence, payload=EXCLUDED.payload, decided_by=EXCLUDED.decided_by""",
                    (
                        tenant,
                        number,
                        f["id"],
                        f.get("line_no"),
                        f["kind"],
                        f["source"],
                        f["decided_by"],
                        f["action"],
                        f["status"],
                        Decimal(str(f.get("adjustment") or 0)),
                        float(f.get("severity") or 0),
                        f.get("confidence"),
                        Jsonb(f),
                    ),
                )

    @staticmethod
    def _summary(number: str, state: dict[str, Any]) -> dict[str, Any]:
        fs = state.get("findings", [])
        return {
            "invoice": number,
            "status": state.get("status"),
            "total_adjustment": state.get("total_adjustment"),
            "findings": len(fs),
            "pending": sum(1 for f in fs if f["status"] == "proposed"),
            "degraded": bool(state.get("degraded")),
            "fatal": state.get("fatal", []),
        }

    # ---------------------------------------------------------------- reads
    async def invoices(self, tenant: str, status: str | None = None, firm: str | None = None) -> list[dict[str, Any]]:
        q = """SELECT i.number, i.firm, i.matter, i.period_start, i.period_end, i.total, i.status, i.total_adjustment, i.degraded,
                      i.received_at, i.settled_at,
                      count(f.id) FILTER (WHERE f.status='proposed') AS pending,
                      coalesce(max(f.severity) FILTER (WHERE f.status='proposed'), 0) AS top_severity,
                      coalesce(sum(-f.adjustment) FILTER (WHERE f.status='proposed'), 0) AS at_stake
               FROM invoices i LEFT JOIN findings f ON f.invoice = i.number AND f.tenant = i.tenant
               WHERE (%(status)s::text IS NULL OR i.status = %(status)s) AND (%(firm)s::text IS NULL OR i.firm = %(firm)s)
               GROUP BY i.tenant, i.number ORDER BY top_severity DESC, i.received_at DESC"""
        async with tenant_tx(self.pool, tenant) as conn:
            return list(await (await conn.execute(q, {"status": status, "firm": firm})).fetchall())

    async def invoice(self, tenant: str, number: str, firm: str | None = None) -> dict[str, Any]:
        async with tenant_tx(self.pool, tenant) as conn:
            row = await (await conn.execute("SELECT * FROM invoices WHERE number=%s", (number,))).fetchone()
            if row is None or (firm and row["firm"] != firm):
                raise NotFound(number)
            fs = await (await conn.execute("SELECT payload FROM findings WHERE invoice=%s ORDER BY severity DESC", (number,))).fetchall()
            appeals = await (await conn.execute("SELECT * FROM appeals WHERE invoice=%s ORDER BY id", (number,))).fetchall()
        inv = ledes.parse(row["ledes"])
        adj = (row["state"] or {}).get("adjustments", {})
        findings = [f["payload"] for f in fs]
        if firm:  # a firm sees the outcome and the explanation, not the internal evidence and model notes
            findings = [
                {k: f[k] for k in ("id", "line_no", "kind", "action", "adjustment", "status", "explanation", "clause")}
                for f in findings
                if f["status"] in ("auto_applied", "accepted")
            ]
            appeals = [{"id": a["id"], "finding_id": a["finding_id"], "outcome": a["outcome"], "message": a["message"]} for a in appeals]
        return {
            "invoice": {
                k: row[k]
                for k in (
                    "number",
                    "firm",
                    "matter",
                    "period_start",
                    "period_end",
                    "total",
                    "status",
                    "total_adjustment",
                    "degraded",
                    "received_at",
                    "settled_at",
                )
            },
            "lines": [li.model_dump(mode="json") | {"adjustment": adj.get(li.line_no, "0")} for li in inv.lines],
            "findings": findings,
            "appeals": appeals,
            "matter": self.ref.matters[inv.client_matter_id].name if inv.client_matter_id in self.ref.matters else inv.client_matter_id,
            "firm_name": self.ref.firms[inv.law_firm_id].name if inv.law_firm_id in self.ref.firms else inv.law_firm_id,
            "currency": self.ref.tenants[tenant].currency,
        }

    # ---------------------------------------------------------------- appeals
    async def appeal(self, tenant: str, number: str, finding_id: str, message: str, firm: str) -> dict[str, Any]:
        detail = await self.invoice(tenant, number)
        if detail["invoice"]["firm"] != firm:
            raise NotFound(number)
        f = next((x for x in detail["findings"] if x["id"] == finding_id), None)
        if f is None or f["status"] not in ("auto_applied", "accepted"):
            raise NotFound(f"no applied adjustment {finding_id}")
        line = next(li for li in detail["lines"] if li["line_no"] == f["line_no"])
        t = self.ref.tenants[tenant]
        graph = build_appeal_graph(AppealDeps(self.nlp, self.router, human="skip"))
        st = await graph.ainvoke(
            {
                "appeal": {
                    "message": message,
                    "finding": f,
                    "description": line["description"],
                    "firm_name": detail["firm_name"],
                    "tenant": tenant,
                    "tenant_name": t.name,
                    "currency": t.currency,
                }
            }
        )
        async with tenant_tx(self.pool, tenant) as conn:
            row = await (
                await conn.execute(
                    """INSERT INTO appeals(tenant, invoice, finding_id, firm, message, intent, recommendation, problems, outcome)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (
                        tenant,
                        number,
                        finding_id,
                        firm,
                        message,
                        st.get("intent"),
                        Jsonb(st.get("recommendation")),
                        Jsonb(st.get("problems", [])),
                        st.get("outcome") or "open",
                    ),
                )
            ).fetchone()
            await self.audit(conn, tenant, f"firm:{firm}", "appeal.received", number, {"finding": finding_id, "intent": st.get("intent")})
        return {
            "id": row["id"] if row else None,
            "intent": st.get("intent"),
            "recommendation": st.get("recommendation"),
            "reply": st.get("reply"),
            "outcome": st.get("outcome"),
        }

    async def decide_appeal(self, tenant: str, appeal_id: int, decision: str, actor: str) -> dict[str, Any]:
        async with tenant_tx(self.pool, tenant) as conn:
            row = await (await conn.execute("SELECT * FROM appeals WHERE id=%s FOR UPDATE", (appeal_id,))).fetchone()
            if row is None:
                raise NotFound(f"appeal {appeal_id}")
            outcome = {"uphold": "upheld", "reverse": "reversed"}[decision]
            await conn.execute("UPDATE appeals SET outcome=%s, decided_by=%s WHERE id=%s", (outcome, actor, appeal_id))
            await self.audit(conn, tenant, actor, "appeal.decided", row["invoice"], {"appeal": appeal_id, "outcome": outcome})
        if decision == "reverse":
            await self.decide(
                tenant, row["invoice"], {row["finding_id"]: {"decision": "reject", "note": f"appeal {appeal_id} reversed"}}, actor
            )
        return {"appeal": appeal_id, "outcome": outcome}

    # ---------------------------------------------------------------- jobs
    async def run_jobs(self, limit: int = 10) -> int:
        """Claim and run queued jobs (FOR UPDATE SKIP LOCKED: any number of workers can share the table)."""
        done = 0
        for _ in range(limit):
            async with system_tx(self.pool) as conn:
                job = await (
                    await conn.execute(
                        """UPDATE jobs SET status='running', attempts=attempts+1, updated_at=now()
                       WHERE id = (SELECT id FROM jobs WHERE status='queued' AND run_after <= now() ORDER BY id
                                   FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *"""
                    )
                ).fetchone()
            if job is None:
                break
            try:
                if job["kind"] == "review":
                    await self.review(job["tenant"], job["payload"]["invoice"])
                status, error = "done", None
            except Exception as e:  # recorded on the job; retried with back-off up to three attempts
                status, error = ("queued" if job["attempts"] < 3 else "failed"), f"{type(e).__name__}: {e}"[:500]
            async with system_tx(self.pool) as conn:
                await conn.execute(
                    "UPDATE jobs SET status=%s, error=%s, updated_at=now(), run_after=now() + (attempts * interval '30 seconds') WHERE id=%s",
                    (status, error, job["id"]),
                )
            done += 1
        return done
