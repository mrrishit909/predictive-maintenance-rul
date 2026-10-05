-- Platform tables shared by every service: tenants, API tokens, idempotency keys, the job queue,
-- the append-only audit chain and the model registry. Forward-only: never edit an applied file.

-- The API never talks to tenant data as the owning role. Each request does SET LOCAL ROLE app_rw
-- and sets app.tenant_id, so the row-level-security policies below apply to every query.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_rw') THEN CREATE ROLE app_rw NOLOGIN; END IF;
END $$;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE ON SEQUENCES TO app_rw;

CREATE FUNCTION enable_tenant_rls(t regclass) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', t);
  EXECUTE format('CREATE POLICY tenant_isolation ON %s
                    USING (tenant_id = current_setting(''app.tenant_id'')::uuid)
                    WITH CHECK (tenant_id = current_setting(''app.tenant_id'')::uuid)', t);
END $$;

CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END $$;

CREATE TABLE tenants (id uuid PRIMARY KEY, name text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());

-- Tokens are stored as SHA-256 only. Looked up before the tenant is known, so no RLS and no app_rw access.
CREATE TABLE api_tokens (
  token_sha256 text PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants,
  actor_id uuid NOT NULL,
  actor_name text NOT NULL,
  role text NOT NULL);
REVOKE ALL ON api_tokens FROM app_rw;

CREATE TABLE idempotency_keys (
  tenant_id uuid NOT NULL REFERENCES tenants,
  key text NOT NULL,
  request_hash text NOT NULL,
  status int,
  body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, key));
SELECT enable_tenant_rls('idempotency_keys');

-- Durable async work. A job is inserted in the same transaction as the state change that needs it
-- (the outbox property), claimed with FOR UPDATE SKIP LOCKED, retried with backoff, and parked as
-- 'dead' with its reason when attempts run out.
CREATE TABLE jobs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants,
  kind text NOT NULL,
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','dead')),
  progress numeric NOT NULL DEFAULT 0,
  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 4,
  run_after timestamptz NOT NULL DEFAULT now(),
  result jsonb,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz);
CREATE INDEX jobs_claim ON jobs (run_after) WHERE status IN ('queued','running');
SELECT enable_tenant_rls('jobs');

CREATE TABLE audit_events (
  id bigserial PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants,
  actor_id uuid NOT NULL,
  action text NOT NULL,
  resource_type text NOT NULL,
  resource_id text NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}',
  at timestamptz NOT NULL,
  prev_hash text NOT NULL,
  hash text NOT NULL);
CREATE INDEX audit_tenant ON audit_events (tenant_id, id);
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit_events FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_events EXECUTE FUNCTION forbid_change();
SELECT enable_tenant_rls('audit_events');

-- Model / solver governance: what was trained, on which data snapshot, how it scored, and the artifact itself.
CREATE TABLE model_registry (
  name text NOT NULL,
  version text NOT NULL,
  state text NOT NULL DEFAULT 'production',
  data_snapshot text NOT NULL,
  metrics jsonb NOT NULL,
  baseline jsonb NOT NULL,
  artifact bytea,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (name, version));
