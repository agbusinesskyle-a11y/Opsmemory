-- OpsMemory — Migration 0018: SOP suggestion engine (Chunk 13).
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns
-- the outer transaction.
--
-- Scope (docs/03-chunk-plan.md:177): year-over-year pattern
-- detection on completed tasks. The detector clusters similar
-- completed tasks across years and surfaces a draft SOP template.
-- Operator promotes via API + PWA → fresh sops + sop_versions
-- (draft) + sop_template_tasks. Existing chunk-7 publish path
-- owns activation; this chunk does NOT auto-publish.
--
-- Per Codex chunk-13 plan-review: cluster_signature is the
-- idempotency key — a deterministic hash over (business_id +
-- normalized seed-task token signatures + month bucket). Re-
-- running the detector with the same data must NOT create
-- duplicate pending suggestions. dismissed-cluster signatures
-- stay in the table so re-detection skips them.

-- =========================================================================
-- sop_suggestion_runs
-- One row per detector invocation. Audit trail + per-run rollup.
-- =========================================================================

CREATE TABLE IF NOT EXISTS sop_suggestion_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  started_at   timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,

  -- 'running' / 'completed' / 'failed'
  status text NOT NULL DEFAULT 'running',

  -- Run scope. NULL = all businesses.
  business_filter text,

  -- Rollup. Updated on completion.
  candidates_evaluated int NOT NULL DEFAULT 0,
  suggestions_created  int NOT NULL DEFAULT 0,
  suggestions_skipped_existing int NOT NULL DEFAULT 0,

  error jsonb NOT NULL DEFAULT '{}'::jsonb,

  CHECK (status IN ('running', 'completed', 'failed')),
  CHECK (jsonb_typeof(error) = 'object')
);

CREATE INDEX IF NOT EXISTS sop_suggestion_runs_started_idx
  ON sop_suggestion_runs(started_at DESC);


-- =========================================================================
-- sop_suggestions
-- One row per detected cluster. UNIQUE on cluster_signature so a
-- nightly re-run is a no-op for already-known clusters.
-- =========================================================================

CREATE TABLE IF NOT EXISTS sop_suggestions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,

  proposed_name        text NOT NULL,
  proposed_description text,

  -- The cluster of historical tasks that motivated the suggestion.
  -- 2..N task ids (typically 2-6).
  seed_task_ids uuid[] NOT NULL,

  -- Rendered draft template, ready to copy into sop_template_tasks
  -- on accept. Each entry mirrors the chunk-7
  -- sop_template_tasks shape: {summary, description?,
  -- due_offset_days, dependency_text?, category?, owner_role?}.
  proposed_template jsonb NOT NULL,

  -- 'pending' (awaiting operator) / 'accepted' (promoted to a
  -- draft sop_version) / 'dismissed' (operator declined; we
  -- remember so re-detection skips this cluster).
  status text NOT NULL DEFAULT 'pending',

  -- Codex chunk-13 plan-review: idempotency key. Deterministic
  -- hash of (business_id, normalized seed-task token
  -- signatures, month bucket). Re-detection with the same
  -- data finds the existing row via this UNIQUE constraint
  -- and skips, so the table doesn't grow each run.
  cluster_signature text NOT NULL,

  -- Set when status flips to 'accepted'. Points at the freshly-
  -- created draft sops row.
  promoted_sop_id uuid REFERENCES sops(id) ON DELETE SET NULL,

  -- Detector run that surfaced this suggestion.
  suggestion_run_id uuid REFERENCES sop_suggestion_runs(id) ON DELETE SET NULL,

  -- Internal: why we suggested it (debug + audit). Free-form
  -- jsonb so future strategies can add structured detail.
  rationale jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- Optional operator-supplied dismiss reason.
  dismissed_reason text,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CHECK (status IN ('pending', 'accepted', 'dismissed')),
  CHECK (jsonb_typeof(proposed_template) = 'array'),
  CHECK (jsonb_typeof(rationale) = 'object'),
  CHECK (array_length(seed_task_ids, 1) >= 2),
  CHECK (length(cluster_signature) BETWEEN 16 AND 128),
  CHECK (length(proposed_name) BETWEEN 1 AND 256),
  UNIQUE (cluster_signature)
);

CREATE INDEX IF NOT EXISTS sop_suggestions_business_status_idx
  ON sop_suggestions(business_id, status);

CREATE INDEX IF NOT EXISTS sop_suggestions_run_idx
  ON sop_suggestions(suggestion_run_id) WHERE suggestion_run_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_sop_suggestions_updated_at
  ON sop_suggestions;
CREATE TRIGGER trg_sop_suggestions_updated_at
  BEFORE UPDATE ON sop_suggestions
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- =========================================================================
-- Runtime grants
-- =========================================================================

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    -- Suggestions are operator-facing artifacts; admin API
    -- needs full CRUD. Detector runs as a different role
    -- (the worker / runner) so it has its own grants below.
    GRANT SELECT, INSERT, UPDATE ON sop_suggestions TO opsmemory_app;
    GRANT SELECT, INSERT, UPDATE ON sop_suggestion_runs TO opsmemory_app;
  END IF;
END $$;
