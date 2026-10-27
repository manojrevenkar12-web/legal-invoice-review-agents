"""PostgreSQL access. Every tenant-scoped query runs in a transaction that has set app.tenant first, so the
row-level security policies in migrations/001_schema.sql apply to it."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

Conn = AsyncConnection[DictRow]
Pool = AsyncConnectionPool[Conn]


class IamAuthConnection(AsyncConnection[DictRow]):
    """Aurora with IAM database authentication: a fresh 15-minute token for every new connection, TLS required."""

    @classmethod
    async def connect(cls, conninfo: str = "", **kwargs: Any) -> Any:
        import boto3

        host, user = os.environ["LIRA_DB_HOST"], os.environ.get("LIRA_DB_USER", "lira_app")
        region = os.environ.get("AWS_REGION", "eu-central-1")
        kwargs["password"] = boto3.client("rds", region_name=region).generate_db_auth_token(host, 5432, user, region)
        return await super().connect(f"host={host} port=5432 dbname=lira user={user} sslmode=verify-full", **kwargs)


def database_url() -> str:
    return os.environ.get("LIRA_DATABASE_URL", "")


async def open_pool(url: str, min_size: int = 1, max_size: int = 10, extra: dict[str, Any] | None = None) -> Pool:
    iam = os.environ.get("LIRA_DB_IAM_AUTH") == "1"
    pool: Pool = AsyncConnectionPool(
        url,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"row_factory": dict_row, **(extra or {})},
        connection_class=IamAuthConnection if iam else Conn,
    )
    await pool.open(wait=True)
    return pool


class UnsafeRole(RuntimeError):
    """The service connected as a role that ignores row-level security."""


async def check_role(pool: Pool) -> None:
    """Superusers and BYPASSRLS roles skip every row-level security policy, even FORCE'd ones. Refuse to serve."""
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")).fetchone()
    if row and (row["rolsuper"] or row["rolbypassrls"]):
        raise UnsafeRole(f"role {row['rolname']} bypasses row-level security; connect as an ordinary role")


async def migrate(pool: Pool) -> list[str]:
    await check_role(pool)
    applied: list[str] = []
    files = sorted((f for f in resources.files("lira.service.migrations").iterdir() if f.name.endswith(".sql")), key=lambda f: f.name)
    async with pool.connection() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        # one migrator at a time across instances
        await conn.execute("SELECT pg_advisory_lock(424242)")
        try:
            done = {r["version"] for r in await (await conn.execute("SELECT version FROM schema_migrations")).fetchall()}
            for f in files:
                if f.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(f.read_text().encode())
                    await conn.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (f.name,))
                applied.append(f.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(424242)")
    return applied


@asynccontextmanager
async def tenant_tx(pool: Pool, tenant: str) -> AsyncIterator[Conn]:
    """A transaction bound to one tenant: row-level security filters every read and checks every write."""
    if not tenant:
        raise ValueError("tenant is required")
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant', %s, true)", (tenant,))
        yield conn


@asynccontextmanager
async def system_tx(pool: Pool) -> AsyncIterator[Conn]:
    """Work that is not tenant data (jobs, tenants, key lookup)."""
    async with pool.connection() as conn, conn.transaction():
        yield conn
