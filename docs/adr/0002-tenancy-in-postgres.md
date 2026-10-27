# 2. Tenant isolation in PostgreSQL, not in application code

**Status:** accepted

**Context.** Several clients' invoices and guidelines live in one service. A missing `WHERE tenant = ...` in one
query would show one client's legal spend to another.

**Decision.** Every tenant table has `FORCE ROW LEVEL SECURITY` with a policy on `current_setting('app.tenant')`.
The application sets it per transaction (`tenant_tx`); a connection without it sees nothing. Retrieval takes the
tenant as a required argument and filters before ranking. Decisions and audit are append-only by trigger.

**Consequences.** Isolation holds even for a query written carelessly, and is tested by writing as one tenant
and reading as another. Work that is not tenant data (jobs, key lookup) uses an explicit `system_tx`.
