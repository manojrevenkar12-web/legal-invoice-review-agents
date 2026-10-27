"""Guideline documents and RuleSets, by tenant and version.

Only approved RuleSets are ever used to review invoices. The version in force for a line is the latest approved
one whose effective date is on or before the date the work was done. Approved RuleSets are immutable: a change
is a new version (or a re-compile of the same version, which must be approved again).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from lira.guidelines.documents import GuidelineDocument, parse
from lira.guidelines.rules import RuleSet


class NoApprovedRules(LookupError):
    pass


class RuleStore:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, tenant: str, version: int) -> Path:
        return self.root / tenant / f"v{version}.json"

    def save(self, rs: RuleSet) -> None:
        p = self._path(rs.tenant, rs.version)
        if p.exists():
            current = RuleSet.model_validate_json(p.read_text())
            if current.status == "approved" and rs.status == "approved" and current.model_dump() != rs.model_dump():
                raise PermissionError(f"{rs.tenant} v{rs.version} is approved and cannot be changed; compile a new version")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rs.model_dump_json(indent=2))

    def get(self, tenant: str, version: int) -> RuleSet | None:
        p = self._path(tenant, version)
        return RuleSet.model_validate_json(p.read_text()) if p.exists() else None

    def all(self, tenant: str) -> list[RuleSet]:
        d = self.root / tenant
        if not d.exists():
            return []
        return sorted((RuleSet.model_validate_json(p.read_text()) for p in d.glob("v*.json")), key=lambda r: r.version)

    def approve(self, tenant: str, version: int, by: str) -> RuleSet:
        rs = self.get(tenant, version)
        if rs is None:
            raise KeyError(f"{tenant} v{version}")
        if rs.status == "approved":
            return rs
        rs.status, rs.approved_by, rs.approved_at = "approved", by, datetime.now(UTC)
        self.save(rs)
        return rs

    def in_force(self, tenant: str, on: date) -> RuleSet:
        approved = [r for r in self.all(tenant) if r.status == "approved" and r.effective_from <= on]
        if not approved:
            raise NoApprovedRules(f"no approved guidelines for {tenant} on {on}")
        return max(approved, key=lambda r: (r.effective_from, r.version))


class DocumentStore:
    """Guideline documents as delivered (Markdown/text), with their version metadata."""

    def __init__(self, root: Path):
        self.root = root

    def versions(self, tenant: str) -> list[int]:
        return sorted(int(p.stem[1:]) for p in (self.root / tenant).glob("v*.md"))

    def get(self, tenant: str, version: int) -> GuidelineDocument:
        meta = json.loads((self.root / tenant / f"v{version}.json").read_text())
        text = (self.root / tenant / f"v{version}.md").read_text()
        return parse(text, tenant, version, date.fromisoformat(meta["effective_from"]))

    def tenants(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir()) if self.root.exists() else []


class MemoryRules:
    """Approved RuleSets held in memory for one run (the service loads them from PostgreSQL per review)."""

    def __init__(self, rulesets: list[RuleSet]):
        self.rulesets = rulesets

    def all(self, tenant: str) -> list[RuleSet]:
        return sorted((r for r in self.rulesets if r.tenant == tenant), key=lambda r: r.version)

    def get(self, tenant: str, version: int) -> RuleSet | None:
        return next((r for r in self.rulesets if r.tenant == tenant and r.version == version), None)

    def in_force(self, tenant: str, on: date) -> RuleSet:
        approved = [r for r in self.all(tenant) if r.status == "approved" and r.effective_from <= on]
        if not approved:
            raise NoApprovedRules(f"no approved guidelines for {tenant} on {on}")
        return max(approved, key=lambda r: (r.effective_from, r.version))
