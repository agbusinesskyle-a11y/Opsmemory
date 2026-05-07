-- OpsMemory — Migration 0017: Weekly Gmail digest drafts (Chunk 11).
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Per docs/01-design.md locked decision: existing Gmail/Calendar tools
-- never auto-fire to customers/vendors from tracker state. Drafts only.
-- Recipient allowlist. Approval gates + idempotency keys + suppression
-- logs before any send.
--
-- Cardinality: per-BUSINESS, NOT per-user. One Gmail DRAFT per business
-- per week summarizing that business's open + completed-this-week +
-- stale tasks. Per-user email_digest is reserved in chunk-10's
-- notification_prefs table but stays unimplemented.

-- =========================================================================
-- weekly_digest_allowlist
-- Operator-managed list of recipient emails per business. Admin-only via
-- /v1/weekly_digest/allowlist endpoints.
-- =========================================================================

CREATE TABLE IF NOT EXISTS weekly_digest_allowlist (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,

  -- citext is case-insensitive comparison + storage so 'Joanna@x' and
  -- 'joanna@x' match without lower() wrappers (Codex chunk-11 plan-
  -- review nit — citext beats UNIQUE on lower()).
  recipient_email citext NOT NULL,

  -- Gmail draft 'role' for this recipient. Drives which header bucket
  -- n8n places them in.
  role text NOT NULL,

  -- Free-form note ("Joanna primary inbox", "kyle CC for awareness").
  notes text,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CHECK (role IN ('to', 'cc', 'bcc')),
  CHECK (position('@' in recipient_email::text) > 1),
  UNIQUE (business_id, recipient_email)
);

CREATE INDEX IF NOT EXISTS weekly_digest_allowlist_business_idx
  ON weekly_digest_allowlist(business_id);

DROP TRIGGER IF EXISTS trg_weekly_digest_allowlist_updated_at
  ON weekly_digest_allowlist;
CREATE TRIGGER trg_weekly_digest_allowlist_updated_at
  BEFORE UPDATE ON weekly_digest_allowlist
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- =========================================================================
-- weekly_digest_runs
-- Audit log + idempotency table for the weekly digest cron. One row per
-- (business, week_start) tuple. The cron does INSERT ... ON CONFLICT
-- (idempotency_key) DO NOTHING to claim, then UPDATEs status/payload as
-- it progresses. Reuses notification_lifecycle_state ENUM from migration
-- 0001 (no new ENUM).
-- =========================================================================

CREATE TABLE IF NOT EXISTS weekly_digest_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,

  -- Both dates anchored to the BUSINESS's local week (Mon..Sun).
  -- week_end_iso is conceptually inclusive of Sunday; we store it
  -- explicitly so audit queries don't have to recompute.
  week_start_iso date NOT NULL,
  week_end_iso   date NOT NULL,

  status notification_lifecycle_state NOT NULL DEFAULT 'pending',

  -- Format: 'weekly_digest:<biz_slug>:<week_start_iso>'.
  -- Length cap matches notification_deliveries for consistency.
  idempotency_key text NOT NULL,

  -- Rendered digest contents that were (or would have been) sent.
  -- {subject, html_body, text_body, to[], cc[], bcc[], counts:{}, items:{}}.
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- {code, http_status?, detail?} on failure. Same shape as
  -- notification_deliveries.error so cross-table audit queries work
  -- uniformly.
  error jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- Gmail draftId returned by n8n on successful drafts.create.
  -- Operators can reconstruct the draft URL from this.
  draft_id text,

  scheduled_for timestamptz NOT NULL,
  attempted_at  timestamptz,
  sent_at       timestamptz,
  failed_at     timestamptz,

  created_at timestamptz NOT NULL DEFAULT now(),

  CHECK (jsonb_typeof(payload) = 'object'),
  CHECK (jsonb_typeof(error)   = 'object'),
  CHECK (length(idempotency_key) BETWEEN 1 AND 256),
  CHECK (week_end_iso >= week_start_iso),
  UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS weekly_digest_runs_business_idx
  ON weekly_digest_runs(business_id, week_start_iso DESC);

CREATE INDEX IF NOT EXISTS weekly_digest_runs_pending_idx
  ON weekly_digest_runs(scheduled_for)
  WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS weekly_digest_runs_status_idx
  ON weekly_digest_runs(status, scheduled_for);


-- =========================================================================
-- Runtime grants
-- =========================================================================

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    -- Allowlist is admin-managed via the API.
    GRANT SELECT, INSERT, UPDATE, DELETE ON weekly_digest_allowlist
      TO opsmemory_app;
    -- Runs are written by the cron + status-updated by the sender.
    -- Admin reads them via /v1/weekly_digest/runs.
    GRANT SELECT, INSERT, UPDATE ON weekly_digest_runs TO opsmemory_app;
  END IF;
END $$;
