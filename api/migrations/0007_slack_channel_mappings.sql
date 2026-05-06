-- OpsMemory — Migration 0007: Slack channel -> business mapping.
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Per Codex chunk-5-step2 plan: channel-name LLM heuristics were too
-- nondeterministic. Replace them with an explicit operator-managed
-- mapping. The slack_resolve module (api/app/reconciliation/) reads
-- this table after extract+normalize and fills candidate.businesses
-- when the message text didn't already name a business.
--
-- Day-1 ops model: admin seeds rows via SQL (psql -v parameterization).
-- A PWA admin UI for managing the mapping lands in a later commit.
--
-- Schema:
--   slack_channel_mappings (
--     id           uuid PK
--     team_id      slack workspace id (T-prefixed)
--     channel_id   slack channel id (C/G/D-prefixed)
--     business_id  uuid REFERENCES businesses(id) ON DELETE RESTRICT
--                  RESTRICT: never silently lose a mapping when a
--                  business is deleted. Operator must drop the
--                  mapping explicitly.
--     channel_name optional human label, for operator readability.
--                  Slack can rename channels; channel_id is the stable
--                  key, name is informational only.
--     status       'active' | 'paused' | 'archived'
--                  paused = mapping exists but resolver should ignore
--                          (e.g. channel currently noisy).
--                  archived = soft-removed; kept for audit.
--     metadata     jsonb, free-form (note, owner-of-mapping, etc.).
--     created_at/by, updated_at/by — audit fields.
--   )
--   UNIQUE (team_id, channel_id) — at most one mapping per channel.
--
-- Acceptance:
--   - slack_channel_mappings exists.
--   - UNIQUE (team_id, channel_id) prevents duplicate mappings.
--   - opsmemory_app has SELECT only (writes happen via psql with
--     opsmemory_owner; admin endpoints add INSERT/UPDATE later).

CREATE TABLE IF NOT EXISTS slack_channel_mappings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id text NOT NULL,
  channel_id text NOT NULL,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE RESTRICT,
  channel_name text,
  status text NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
  CHECK (length(team_id) BETWEEN 2 AND 32),
  CHECK (length(channel_id) BETWEEN 2 AND 32),
  CHECK (status IN ('active', 'paused', 'archived')),
  CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE UNIQUE INDEX IF NOT EXISTS slack_channel_mappings_team_channel_uidx
  ON slack_channel_mappings(team_id, channel_id);

CREATE INDEX IF NOT EXISTS slack_channel_mappings_business_idx
  ON slack_channel_mappings(business_id);

DROP TRIGGER IF EXISTS trg_slack_channel_mappings_updated_at ON slack_channel_mappings;
CREATE TRIGGER trg_slack_channel_mappings_updated_at
BEFORE UPDATE ON slack_channel_mappings
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    GRANT SELECT ON slack_channel_mappings TO opsmemory_app;
  END IF;
END $$;
