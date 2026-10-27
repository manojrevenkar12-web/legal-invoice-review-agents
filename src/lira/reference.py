"""Reference data from the client's legal-management system: tenants, firms and timekeepers, matters, rate agreements.

In production this comes from the matter-management database (PostgreSQL); in the simulation from data/world.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from lira.domain import Firm, Matter, RateAgreement, Tenant


@dataclass
class Reference:
    tenants: dict[str, Tenant]
    firms: dict[str, Firm]
    matters: dict[str, Matter]
    rates: list[RateAgreement]

    @classmethod
    def load(cls, world: Path) -> Reference:
        def rd(name: str) -> list[dict]:
            return json.loads((world / name).read_text())

        return cls(
            tenants={t["id"]: Tenant.model_validate(t) for t in rd("tenants.json")},
            firms={f["id"]: Firm.model_validate(f) for f in rd("firms.json")},
            matters={m["id"]: Matter.model_validate(m) for m in rd("matters.json")},
            rates=[RateAgreement.model_validate(r) for r in rd("rate_agreements.json")],
        )

    def tenant_for_client(self, client_id: str) -> Tenant | None:
        return next((t for t in self.tenants.values() if t.client_id == client_id), None)

    def approved_rate(self, tenant: str, timekeeper_id: str, on: date) -> Decimal | None:
        found = [r for r in self.rates if r.tenant == tenant and r.timekeeper_id == timekeeper_id and r.covers(on)]
        return max(found, key=lambda r: r.effective_from).rate if found else None

    def colleagues(self, firm_id: str) -> list[tuple[str, str]]:
        """(timekeeper id, name) for everyone at the firm, used to resolve names in time entries."""
        f = self.firms.get(firm_id)
        return [(t.id, t.name) for t in f.timekeepers] if f else []
