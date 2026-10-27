-- Legal invoice review: schema. Every tenant-owned table has row-level security keyed on the transaction
-- setting app.tenant, FORCEd so that the table owner (the application role) is subject to it too. The service
-- sets app.tenant from the authenticated API key at the start of every transaction; a query that forgets the
-- tenant sees nothing rather than everything.

CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS tenants (
    id text PRIMARY KEY,
    name text NOT NULL,
    client_id text NOT NULL UNIQUE,
    currency text NOT NULL
);

-- API keys are looked up before a tenant is known, so this table is read through a SECURITY DEFINER function only.
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash text PRIMARY KEY,
    tenant text NOT NULL REFERENCES tenants(id),
    role text NOT NULL CHECK (role IN ('reviewer', 'legal_ops', 'firm', 'admin')),
    firm text,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

CREATE TABLE IF NOT EXISTS rulesets (
    tenant text NOT NULL REFERENCES tenants(id),
    version int NOT NULL,
    status text NOT NULL CHECK (status IN ('draft', 'approved', 'superseded')),
    document_sha256 text NOT NULL,
    effective_from date NOT NULL,
    payload jsonb NOT NULL,
    compile_report jsonb,
    document text NOT NULL,
    approved_by text,
    approved_at timestamptz,
    PRIMARY KEY (tenant, version)
);

CREATE TABLE IF NOT EXISTS invoices (
    tenant text NOT NULL REFERENCES tenants(id),
    number text NOT NULL,
    firm text NOT NULL,
    matter text NOT NULL,
    period_start date,
    period_end date,
    total numeric(14, 2) NOT NULL,
    status text NOT NULL,
    ledes text NOT NULL,
    total_adjustment numeric(14, 2),
    degraded boolean NOT NULL DEFAULT false,
    model_cost_usd double precision NOT NULL DEFAULT 0,
    received_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    state jsonb,
    PRIMARY KEY (tenant, number)
);

CREATE TABLE IF NOT EXISTS findings (
    tenant text NOT NULL,
    invoice text NOT NULL,
    id text NOT NULL,
    line_no text,
    kind text NOT NULL,
    source text NOT NULL,
    decided_by text NOT NULL,
    action text NOT NULL,
    status text NOT NULL,
    adjustment numeric(14, 2) NOT NULL,
    severity double precision NOT NULL DEFAULT 0,
    confidence double precision,
    payload jsonb NOT NULL,
    PRIMARY KEY (tenant, id),
    FOREIGN KEY (tenant, invoice) REFERENCES invoices(tenant, number)
);
CREATE INDEX IF NOT EXISTS findings_queue ON findings (tenant, status, severity DESC);

-- Decisions and the audit trail are append-only: the application role may insert and read, never update or delete.
CREATE TABLE IF NOT EXISTS decisions (
    id bigserial PRIMARY KEY,
    tenant text NOT NULL,
    invoice text NOT NULL,
    finding_id text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('accept', 'reject')),
    by_user text NOT NULL,
    note text,
    at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS appeals (
    id bigserial PRIMARY KEY,
    tenant text NOT NULL,
    invoice text NOT NULL,
    finding_id text NOT NULL,
    firm text NOT NULL,
    message text NOT NULL,
    intent text,
    recommendation jsonb,
    problems jsonb,
    outcome text NOT NULL DEFAULT 'open',
    decided_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    id bigserial PRIMARY KEY,
    tenant text NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts int NOT NULL DEFAULT 0,
    run_after timestamptz NOT NULL DEFAULT now(),
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (status, run_after);

CREATE TABLE IF NOT EXISTS audit (
    id bigserial PRIMARY KEY,
    tenant text NOT NULL,
    at timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL,
    action text NOT NULL,
    object text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['rulesets', 'invoices', 'findings', 'decisions', 'appeals', 'audit'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant = current_setting(''app.tenant'', true)) '
                       'WITH CHECK (tenant = current_setting(''app.tenant'', true))', t);
    END LOOP;
END $$;

-- append-only for the application role (a migration role owns schema changes in production)
CREATE OR REPLACE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END $$;
DROP TRIGGER IF EXISTS decisions_append_only ON decisions;
CREATE TRIGGER decisions_append_only BEFORE UPDATE OR DELETE ON decisions FOR EACH ROW EXECUTE FUNCTION forbid_change();
DROP TRIGGER IF EXISTS audit_append_only ON audit;
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit FOR EACH ROW EXECUTE FUNCTION forbid_change();

-- key lookup without a tenant context
CREATE OR REPLACE FUNCTION api_key_lookup(h text)
RETURNS TABLE (tenant text, role text, firm text, name text) LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT tenant, role, firm, name FROM api_keys WHERE key_hash = h AND revoked_at IS NULL
$$;
