"""Seed a database with the simulated world for the console demo and the end-to-end tests.

Demo keys are fixed strings and exist only when this seeding runs (never in production):
    demo-reviewer-<tenant>, demo-legalops-<tenant>, demo-firm-<firm>-<tenant>
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from lira import config
from lira.service.auth import create_key
from lira.service.core import Services


async def seed(svc: Services, data: Path | None = None, invoices_from: str = "2026-08", decide_share: float = 0.35) -> dict[str, Any]:
    data = data or config.data_dir()
    await svc.seed_reference()
    keys: dict[str, str] = {}
    for t in svc.ref.tenants:
        keys[f"reviewer:{t}"] = await create_key(svc.pool, t, "reviewer", f"Reviewer ({t})", key=f"demo-reviewer-{t}")
        keys[f"legal_ops:{t}"] = await create_key(svc.pool, t, "legal_ops", f"Legal operations ({t})", key=f"demo-legalops-{t}")
        firms = {m.firm_id for m in svc.ref.matters.values() if m.tenant == t}
        for f in sorted(firms):
            keys[f"firm:{f}:{t}"] = await create_key(svc.pool, t, "firm", svc.ref.firms[f].name, firm=f, key=f"demo-firm-{f}-{t}")
    gdir = data / "world" / "guidelines"
    for t in svc.ref.tenants:
        for md in sorted((gdir / t).glob("v*.md")):
            v = int(md.stem[1:])
            meta = json.loads((gdir / t / f"v{v}.json").read_text())
            existing = {r.version: r for r in await svc.rulesets(t)}
            if v in existing and existing[v].status == "approved":
                continue
            await svc.upload_guidelines(t, v, date.fromisoformat(meta["effective_from"]), md.read_text(), "Legal operations (demo)")
            await svc.approve_guidelines(t, v, "Legal operations (demo)")
    received = 0
    files = sorted(p for p in (data / "world" / "invoices").glob("*/*/*.ledes") if p.parent.name >= invoices_from)
    for p in files:
        out = await svc.receive(p.parent.parent.name, p.read_text(), "seed")
        received += out.get("status") == "queued"
    while await svc.run_jobs(50):
        pass
    decided = 0
    for t in svc.ref.tenants:
        rows = await svc.invoices(t, "awaiting_review")
        for r in rows[: int(len(rows) * decide_share)]:
            d = await svc.invoice(t, r["number"])
            pending = [f for f in d["findings"] if f["status"] == "proposed"]
            # a reviewer who accepts confident findings and rejects the rest, so the demo shows both outcomes
            decisions = {
                f["id"]: {"decision": "accept" if (f.get("confidence") or 0) >= 0.85 else "reject", "note": "demo"} for f in pending
            }
            if decisions:
                await svc.decide(t, r["number"], decisions, f"Reviewer ({t})")
                decided += 1
    return {"invoices": received, "decided": decided, "keys": sorted(keys)}
