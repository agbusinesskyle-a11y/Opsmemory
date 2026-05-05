-- OpsMemory — Migration 0001: Substrate
--
-- Idempotent. Safe to run repeatedly.
-- Run as `opsmemory_owner` (creates schema). Runtime app uses `opsmemory_app` (limited privileges).
--
-- Acceptance:
--   - 7 ENUMs: app_role, user_status, task_lifecycle_state, review_lifecycle_state,
--     ingest_lifecycle_state, notification_lifecycle_state, deletion_lifecycle_state
--   - Tables: schema_migrations, businesses, users, user_identities, business_memberships,
--     service_accounts, task_state_transitions
--   - Seed: 4 users, 2 businesses, 6 business_memberships, 4 user_identities (Cloudflare Access)
--   - Triggers: updated_at on all mutable tables

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- =========================================================================
-- ENUMs (5 lifecycles + identity types)
-- =========================================================================

DO $$ BEGIN
  CREATE TYPE app_role AS ENUM ('admin', 'owner', 'service');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE user_status AS ENUM ('active', 'disabled');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE task_lifecycle_state AS ENUM ('open', 'done');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE review_lifecycle_state AS ENUM (
    'pending',
    'approved',
    'rejected',
    'needs_changes',
    'superseded'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE ingest_lifecycle_state AS ENUM (
    'received',
    'extracting',
    'pending_review',
    'completed',
    'failed',
    'cancelled'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE notification_lifecycle_state AS ENUM (
    'pending',
    'scheduled',
    'sent',
    'failed',
    'cancelled',
    'suppressed'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE deletion_lifecycle_state AS ENUM (
    'active',
    'soft_deleted',
    'restore_requested',
    'restored',
    'hard_delete_eligible',
    'hard_deleted'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- =========================================================================
-- schema_migrations bookkeeping
-- =========================================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY,
  description text NOT NULL,
  checksum text,
  applied_at timestamptz NOT NULL DEFAULT now(),
  execution_ms integer,
  dirty boolean NOT NULL DEFAULT false
);

-- =========================================================================
-- businesses
-- =========================================================================

CREATE TABLE IF NOT EXISTS businesses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug citext NOT NULL UNIQUE,
  name text NOT NULL,
  deletion_state deletion_lifecycle_state NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  CHECK (slug::text ~ '^[a-z0-9][a-z0-9-]*$'),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (
    (deletion_state = 'active' AND deleted_at IS NULL)
    OR deletion_state <> 'active'
  )
);

-- =========================================================================
-- users
-- =========================================================================

CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email citext NOT NULL UNIQUE,
  display_name text NOT NULL,
  role app_role NOT NULL,
  status user_status NOT NULL DEFAULT 'active',
  timezone text NOT NULL DEFAULT 'America/Phoenix',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz,
  CHECK (role IN ('admin', 'owner')),
  CHECK (position('@' in email::text) > 1),
  CHECK (jsonb_typeof(metadata) = 'object')
);

-- =========================================================================
-- user_identities (provider subject mapping)
-- =========================================================================

CREATE TABLE IF NOT EXISTS user_identities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider text NOT NULL,
  provider_subject text,
  email citext NOT NULL,
  claims jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_authenticated_at timestamptz,
  CHECK (provider IN ('cloudflare_access', 'google', 'slack')),
  CHECK (position('@' in email::text) > 1),
  CHECK (jsonb_typeof(claims) = 'object')
);

CREATE UNIQUE INDEX IF NOT EXISTS user_identities_provider_subject_uidx
  ON user_identities(provider, provider_subject)
  WHERE provider_subject IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS user_identities_provider_email_uidx
  ON user_identities(provider, email);

CREATE INDEX IF NOT EXISTS user_identities_user_id_idx
  ON user_identities(user_id);

-- =========================================================================
-- business_memberships
-- =========================================================================

CREATE TABLE IF NOT EXISTS business_memberships (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role app_role NOT NULL,
  status user_status NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (role IN ('admin', 'owner')),
  UNIQUE (business_id, user_id)
);

CREATE INDEX IF NOT EXISTS business_memberships_user_id_idx
  ON business_memberships(user_id);

-- =========================================================================
-- service_accounts (n8n, doc-processor, MCP)
-- =========================================================================

CREATE TABLE IF NOT EXISTS service_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  description text,
  role app_role NOT NULL DEFAULT 'service',
  status user_status NOT NULL DEFAULT 'active',
  key_prefix text NOT NULL UNIQUE,
  key_hash text NOT NULL,
  scopes text[] NOT NULL DEFAULT ARRAY[]::text[],
  created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  expires_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CHECK (role = 'service'),
  CHECK (length(key_prefix) >= 8),
  CHECK (key_hash ~ '^[a-f0-9]{64}$'),
  CHECK (jsonb_typeof(metadata) = 'object')
);

-- =========================================================================
-- task_state_transitions (cross-table state-change audit log)
-- task_id is unconstrained until tasks table arrives in Chunk 2.
-- =========================================================================

CREATE TABLE IF NOT EXISTS task_state_transitions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE RESTRICT,
  from_state task_lifecycle_state,
  to_state task_lifecycle_state NOT NULL,
  actor_kind text NOT NULL,
  actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  actor_service_account_id uuid REFERENCES service_accounts(id) ON DELETE SET NULL,
  reason text,
  request_id text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  transitioned_at timestamptz NOT NULL DEFAULT now(),
  CHECK (from_state IS NULL OR from_state <> to_state),
  CHECK (actor_kind IN ('user', 'service', 'system', 'migration')),
  CHECK (
    (actor_kind = 'user' AND actor_user_id IS NOT NULL AND actor_service_account_id IS NULL)
    OR (actor_kind = 'service' AND actor_user_id IS NULL AND actor_service_account_id IS NOT NULL)
    OR (actor_kind IN ('system', 'migration') AND actor_user_id IS NULL AND actor_service_account_id IS NULL)
  ),
  CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE INDEX IF NOT EXISTS task_state_transitions_task_id_idx
  ON task_state_transitions(task_id);

CREATE INDEX IF NOT EXISTS task_state_transitions_business_time_idx
  ON task_state_transitions(business_id, transitioned_at DESC);

-- =========================================================================
-- updated_at trigger function + per-table triggers
-- =========================================================================

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

-- DROP-then-CREATE pattern guarantees per-table idempotency (the previous
-- pg_trigger lookup by name alone could collide across tables).
DROP TRIGGER IF EXISTS trg_businesses_updated_at ON businesses;
CREATE TRIGGER trg_businesses_updated_at
BEFORE UPDATE ON businesses
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at
BEFORE UPDATE ON users
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_user_identities_updated_at ON user_identities;
CREATE TRIGGER trg_user_identities_updated_at
BEFORE UPDATE ON user_identities
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_business_memberships_updated_at ON business_memberships;
CREATE TRIGGER trg_business_memberships_updated_at
BEFORE UPDATE ON business_memberships
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_service_accounts_updated_at ON service_accounts;
CREATE TRIGGER trg_service_accounts_updated_at
BEFORE UPDATE ON service_accounts
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =========================================================================
-- Seed data (idempotent — uses ON CONFLICT)
-- =========================================================================

-- Businesses
INSERT INTO businesses (id, slug, name)
VALUES
  ('00000000-0000-0000-0000-000000000201', 'redhot', 'RedHot Fireworks'),
  ('00000000-0000-0000-0000-000000000202', 'borderline', 'Borderline Fireworks')
ON CONFLICT (id) DO UPDATE
SET slug = EXCLUDED.slug,
    name = EXCLUDED.name,
    updated_at = now();

-- Users (real Google emails — Kyle, Joanna, Caleb, Sarah)
INSERT INTO users (id, email, display_name, role)
VALUES
  ('00000000-0000-0000-0000-000000000101', 'agbusiness.kyle@gmail.com', 'Kyle Conway', 'admin'),
  ('00000000-0000-0000-0000-000000000102', 'joanna@borderlinefireworksoutlet.com', 'Joanna Noriega', 'admin'),
  ('00000000-0000-0000-0000-000000000103', 'noriega3636@gmail.com', 'Caleb Noriega', 'owner'),
  ('00000000-0000-0000-0000-000000000104', 'sarahjconway@gmail.com', 'Sarah Conway', 'owner')
ON CONFLICT (id) DO UPDATE
SET email = EXCLUDED.email,
    display_name = EXCLUDED.display_name,
    role = EXCLUDED.role,
    updated_at = now();

-- User identities (Cloudflare Access provider, email-only — provider_subject filled at first login)
INSERT INTO user_identities (user_id, provider, provider_subject, email)
SELECT id, 'cloudflare_access', NULL, email
FROM users
ON CONFLICT (provider, email) DO UPDATE
SET user_id = EXCLUDED.user_id,
    updated_at = now();

-- Business memberships
-- Kyle: admin on both businesses
-- Joanna: admin on both businesses
-- Caleb: owner of RedHot only
-- Sarah: owner of Borderline only
INSERT INTO business_memberships (business_id, user_id, role)
VALUES
  ('00000000-0000-0000-0000-000000000201', '00000000-0000-0000-0000-000000000101', 'admin'),
  ('00000000-0000-0000-0000-000000000202', '00000000-0000-0000-0000-000000000101', 'admin'),
  ('00000000-0000-0000-0000-000000000201', '00000000-0000-0000-0000-000000000102', 'admin'),
  ('00000000-0000-0000-0000-000000000202', '00000000-0000-0000-0000-000000000102', 'admin'),
  ('00000000-0000-0000-0000-000000000201', '00000000-0000-0000-0000-000000000103', 'owner'),
  ('00000000-0000-0000-0000-000000000202', '00000000-0000-0000-0000-000000000104', 'owner')
ON CONFLICT (business_id, user_id) DO UPDATE
SET role = EXCLUDED.role,
    updated_at = now();

-- =========================================================================
-- Migration bookkeeping
-- =========================================================================

INSERT INTO schema_migrations (version, description)
VALUES ('0001_initial', 'Chunk 1 substrate: identity, businesses, lifecycle enums, audit log, seed data')
ON CONFLICT (version) DO UPDATE
SET description = EXCLUDED.description,
    applied_at = now(),
    dirty = false;

-- =========================================================================
-- Grant narrow runtime privileges to opsmemory_app (if role exists).
-- Chunk 1 runtime never DELETEs from any table — every "deletion" is a
-- soft-delete UPDATE setting deleted_at. schema_migrations is read-only
-- to the runtime; the migration script (run as opsmemory_owner) is the
-- only writer.
-- =========================================================================

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    GRANT USAGE ON SCHEMA public TO opsmemory_app;

    -- schema_migrations: read-only.
    GRANT SELECT ON schema_migrations TO opsmemory_app;

    -- Identity + business tables: SELECT + UPDATE only (last_seen_at,
    -- last_authenticated_at, claims, provider_subject upgrade). No INSERT
    -- because seeding happens via migrations; no DELETE because soft-delete
    -- is via UPDATE of deletion_state + deleted_at.
    GRANT SELECT, UPDATE ON users TO opsmemory_app;
    GRANT SELECT, UPDATE ON user_identities TO opsmemory_app;
    GRANT SELECT, UPDATE ON businesses TO opsmemory_app;
    GRANT SELECT, UPDATE ON business_memberships TO opsmemory_app;
    GRANT SELECT, UPDATE ON service_accounts TO opsmemory_app;

    -- Audit log: SELECT + INSERT only. Never UPDATE/DELETE.
    GRANT SELECT, INSERT ON task_state_transitions TO opsmemory_app;

    -- Sequences (used by uuid PKs are gen_random_uuid() — no sequences yet,
    -- but ALTER DEFAULT PRIVILEGES sets up future tables to inherit a sane
    -- default). Restricting future tables to SELECT/INSERT/UPDATE — DELETE
    -- on new tables must be granted explicitly per table when added.
    GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO opsmemory_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE ON TABLES TO opsmemory_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT USAGE ON SEQUENCES TO opsmemory_app;
  END IF;
END $$;

COMMIT;
