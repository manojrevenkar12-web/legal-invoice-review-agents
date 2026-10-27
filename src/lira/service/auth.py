"""API keys: only a SHA-256 of each key is stored; a key carries its tenant, role and (for law firms) firm.

Roles: reviewer (decides findings), legal_ops (uploads and approves guidelines), firm (submits invoices and
appeals, sees only its own invoices), admin (keys). Behind the AWS load balancer, the same principal can come
from a Cognito/OIDC token; the role model is the same.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from lira.service.db import Pool, system_tx


@dataclass(frozen=True)
class Principal:
    tenant: str
    role: str
    name: str
    firm: str | None = None


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    return "lira_" + secrets.token_urlsafe(32)


async def create_key(pool: Pool, tenant: str, role: str, name: str, firm: str | None = None, key: str | None = None) -> str:
    key = key or new_key()
    async with system_tx(pool) as conn:
        await conn.execute(
            "INSERT INTO api_keys(key_hash, tenant, role, firm, name) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (hash_key(key), tenant, role, firm, name),
        )
    return key


async def principal(request: Request) -> Principal:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    pool: Pool = request.app.state.pool
    async with system_tx(pool) as conn:
        row = await (await conn.execute("SELECT * FROM api_key_lookup(%s)", (hash_key(header[7:].strip()),))).fetchone()
    if row is None:
        raise HTTPException(401, "invalid or revoked key")
    return Principal(row["tenant"], row["role"], row["name"], row["firm"])


def require(*roles: str):
    async def dep(p: Principal = Depends(principal)) -> Principal:
        if p.role not in roles and p.role != "admin":
            raise HTTPException(403, f"requires role {' or '.join(roles)}")
        return p

    return dep
