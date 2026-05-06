-- OpsMemory — Migration 0009: SOP schema (Chunk 7 step 1).
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Per Codex chunk-6-close STEP 7 PLAN: SOPs (Standard Operating
-- Procedures) ride on top of the existing review pipeline. SOP
-- materialization creates ingest_events.source='sop_anchor' plus
-- review_items.proposed_action='CREATE_TASK' — no parallel lifecycle.
-- This migration ships ONLY the schema (tables, indexes, FKs, ENUMs,
-- CHECKs, runtime SELECT grants). Behavior (CRUD endpoints, anchor
-- fire, materialization, PWA tab) lands in subsequent commits.
--
-- Tables added:
--   sops                  Top-level SOP record (per business). Points
--                         at the latest published version.
--   sop_versions          Immutable-once-published versions of an SOP.
--                         draft -> published -> superseded.
--   sop_template_tasks    The tasks each SOP version generates when
--                         materialized. Per-template due_offset_days,
--                         dependency_text, owner_role.
--   anchor_events         When/why an SOP fires (e.g. "RedHot Opening
--                         2026-09-01"). One anchor per business+kind+
--                         scheduled_for. Points at the SOP it triggers.
--   sop_instances         One row per anchor-fire. Links anchor +
--                         sop_version + the ingest_event the
--                         materialization created.
--   sop_generated_tasks   Junction: each template task -> review_item
--                         row from materialization. Carries
--                         manually_overridden_fields for the chunk-1
--                         design's per-field human-override flag.
--
-- ENUMs added:
--   sop_status            'active' | 'archived'
--   sop_version_state     'draft' | 'published' | 'superseded'
--   anchor_event_state    'scheduled' | 'fired' | 'cancelled' | 'failed'

-- =========================================================================
-- ENUMs
-- =========================================================================

DO $$ BEGIN
  CREATE TYPE sop_status AS ENUM ('active', 'archived');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE sop_version_state AS ENUM ('draft', 'published', 'superseded');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE anchor_event_state AS ENUM ('scheduled', 'fired', 'cancelled', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- =========================================================================
-- sops
-- =========================================================================

CREATE TABLE IF NOT EXISTS sops (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE RESTRICT,

  name text NOT NULL,
  description text,

  status sop_status NOT NULL DEFAULT 'active',

  -- Set ONLY by the publish transaction. Nullable so a freshly created
  -- SOP with no published version can exist (drafts only). FK added
  -- below as a separate ALTER (circular dep with sop_versions).
  latest_version_id uuid,

  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id) ON DELETE SET NULL,

  CHECK (length(name) BETWEEN 1 AND 256),
  CHECK (jsonb_typeof(metadata) = 'object'),
  -- Per business, SOP names should be unique enough for operator
  -- readability; case-folded uniqueness later if needed. Soft uniqueness
  -- via index, not constraint, so a renamed-and-archived SOP can
  -- coexist with a same-named replacement.
  UNIQUE (business_id, name)
);

CREATE INDEX IF NOT EXISTS sops_business_idx ON sops(business_id);
CREATE INDEX IF NOT EXISTS sops_status_idx ON sops(status);

-- =========================================================================
-- sop_versions
-- =========================================================================

CREATE TABLE IF NOT EXISTS sop_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  sop_id uuid NOT NULL REFERENCES sops(id) ON DELETE CASCADE,

  -- Monotonic per-sop. Always starts at 1 for the first draft;
  -- application code allocates next via SELECT max(...)+1 inside the
  -- write txn.
  version_no int NOT NULL,

  state sop_version_state NOT NULL DEFAULT 'draft',

  -- Free-form description of what changed vs the prior published
  -- version. Required at publish time, optional in draft.
  change_log text,

  -- Audit
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  published_at timestamptz,
  published_by uuid REFERENCES users(id) ON DELETE SET NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id) ON DELETE SET NULL,

  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

  CHECK (version_no >= 1),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (
    -- published rows must have a publisher + timestamp
    (state <> 'published') OR
    (published_at IS NOT NULL AND published_by IS NOT NULL)
  ),
  UNIQUE (sop_id, version_no)
);

CREATE INDEX IF NOT EXISTS sop_versions_sop_idx ON sop_versions(sop_id);
CREATE INDEX IF NOT EXISTS sop_versions_state_idx
  ON sop_versions(state, sop_id) WHERE state IN ('draft', 'published');

-- =========================================================================
-- sops.latest_version_id FK (circular dep resolved here)
-- =========================================================================

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE table_name = 'sops'
      AND constraint_name = 'sops_latest_version_id_fkey'
  ) THEN
    ALTER TABLE sops
    ADD CONSTRAINT sops_latest_version_id_fkey
    FOREIGN KEY (latest_version_id) REFERENCES sop_versions(id) ON DELETE SET NULL;
  END IF;
END $$;

-- =========================================================================
-- sop_template_tasks
-- =========================================================================

CREATE TABLE IF NOT EXISTS sop_template_tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  sop_version_id uuid NOT NULL REFERENCES sop_versions(id) ON DELETE CASCADE,

  -- Stable ordering within a version (operator-controlled).
  seq_no int NOT NULL,

  summary text NOT NULL,
  description text,

  -- Days offset from the anchor_event.scheduled_for date. Positive =
  -- after anchor, negative = before anchor (e.g. "order containers 30
  -- days before opening day").
  due_offset_days int,

  dependency_text text,
  category text,
  priority text,

  -- Owner role: 'admin', 'owner', 'unassigned'. Free text initially;
  -- enforced by future application validation. Can be NULL meaning
  -- "anyone can pick this up".
  owner_role text,
  -- Optional explicit user override. When set, materialization assigns
  -- this user instead of resolving by role. ON DELETE SET NULL so
  -- removing a user doesn't cascade-delete templates.
  owner_user_id uuid REFERENCES users(id) ON DELETE SET NULL,

  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CHECK (seq_no >= 0),
  CHECK (length(summary) BETWEEN 1 AND 4096),
  CHECK (priority IS NULL OR length(priority) BETWEEN 1 AND 32),
  CHECK (owner_role IS NULL OR length(owner_role) BETWEEN 1 AND 64),
  CHECK (jsonb_typeof(metadata) = 'object'),
  -- Stable order: each (version, seq_no) is one template.
  UNIQUE (sop_version_id, seq_no)
);

CREATE INDEX IF NOT EXISTS sop_template_tasks_version_idx
  ON sop_template_tasks(sop_version_id);

-- =========================================================================
-- anchor_events
-- =========================================================================

CREATE TABLE IF NOT EXISTS anchor_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE RESTRICT,

  -- Operator-meaningful kind, e.g. 'redhot_opening', 'borderline_4th'.
  -- Free text + length check initially; CHECK constraint can tighten to
  -- a known set later.
  kind text NOT NULL,

  -- The SOP this anchor materializes when fired. The SOP's
  -- latest_version_id is read at fire time (decided in materialization
  -- code, not enforced in schema).
  sop_id uuid NOT NULL REFERENCES sops(id) ON DELETE RESTRICT,

  -- Date the SOP "happens at". sop_template_tasks.due_offset_days are
  -- relative to this. Time-of-day deliberately optional — many
  -- anchors are date-only events.
  scheduled_for timestamptz NOT NULL,

  state anchor_event_state NOT NULL DEFAULT 'scheduled',
  fired_at timestamptz,
  fired_by uuid REFERENCES users(id) ON DELETE SET NULL,
  cancelled_at timestamptz,
  cancelled_by uuid REFERENCES users(id) ON DELETE SET NULL,

  notes text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id) ON DELETE SET NULL,

  CHECK (length(kind) BETWEEN 1 AND 64),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (
    -- 'fired' state requires fired_at + fired_by
    (state <> 'fired') OR (fired_at IS NOT NULL AND fired_by IS NOT NULL)
  ),
  CHECK (
    -- 'cancelled' state requires cancelled_at + cancelled_by
    (state <> 'cancelled') OR (cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL)
  ),
  -- One scheduled anchor per (business, kind, scheduled_for) — prevents
  -- accidental dup-fire of the same opening day.
  UNIQUE (business_id, kind, scheduled_for)
);

CREATE INDEX IF NOT EXISTS anchor_events_business_idx ON anchor_events(business_id);
CREATE INDEX IF NOT EXISTS anchor_events_sop_idx ON anchor_events(sop_id);
CREATE INDEX IF NOT EXISTS anchor_events_state_idx ON anchor_events(state, scheduled_for);

-- =========================================================================
-- sop_instances
-- =========================================================================

CREATE TABLE IF NOT EXISTS sop_instances (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  anchor_event_id uuid NOT NULL REFERENCES anchor_events(id) ON DELETE RESTRICT,

  -- The exact SOP version that was materialized (frozen reference, not
  -- "latest" — published versions are immutable, so re-firing always
  -- generates the same template tasks).
  sop_version_id uuid NOT NULL REFERENCES sop_versions(id) ON DELETE RESTRICT,

  -- Materialization writes one ingest_events row + N review_items.
  -- The link here is the bridge to the existing reconciliation pipeline.
  -- Nullable until the materialization commits; FK ensures we never
  -- end up with a hanging instance pointing at a deleted ingest_event.
  ingest_event_id uuid REFERENCES ingest_events(id) ON DELETE SET NULL,

  fired_at timestamptz NOT NULL DEFAULT now(),
  fired_by uuid REFERENCES users(id) ON DELETE SET NULL,

  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

  CHECK (jsonb_typeof(metadata) = 'object'),
  -- Per Codex chunk-6-close STEP 7 PLAN: one fire per
  -- (anchor_event, sop_version). Re-firing the same SOP from the same
  -- anchor is a noop / 409, not a duplicate materialization.
  UNIQUE (anchor_event_id, sop_version_id)
);

CREATE INDEX IF NOT EXISTS sop_instances_anchor_idx ON sop_instances(anchor_event_id);
CREATE INDEX IF NOT EXISTS sop_instances_version_idx ON sop_instances(sop_version_id);
CREATE INDEX IF NOT EXISTS sop_instances_ingest_idx
  ON sop_instances(ingest_event_id) WHERE ingest_event_id IS NOT NULL;

-- =========================================================================
-- sop_generated_tasks
-- =========================================================================

CREATE TABLE IF NOT EXISTS sop_generated_tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  sop_instance_id uuid NOT NULL REFERENCES sop_instances(id) ON DELETE CASCADE,
  sop_template_task_id uuid NOT NULL REFERENCES sop_template_tasks(id) ON DELETE RESTRICT,

  -- The review_item the materialization created. Always populated at
  -- materialization commit. ON DELETE SET NULL because review_items
  -- can be admin-deleted later without losing the audit trail of
  -- what the SOP generated.
  review_item_id uuid REFERENCES review_items(id) ON DELETE SET NULL,

  -- The task that was eventually created from approving the
  -- review_item. NULL until approved. Used by date-shift propagation
  -- (chunk 7 step N): when an anchor's scheduled_for changes, we walk
  -- sop_generated_tasks where task_id IS NOT NULL and bump the due_at
  -- on tasks whose corresponding fields are NOT in
  -- manually_overridden_fields.
  task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,

  -- Per-field human-override flags from the chunk-1 design (decision 2):
  -- when a human edits a SOP-materialized task's field, the field's
  -- name lands here so subsequent SOP propagation skips it.
  manually_overridden_fields jsonb NOT NULL DEFAULT '[]'::jsonb,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CHECK (jsonb_typeof(manually_overridden_fields) = 'array'),
  -- One row per (instance, template). Re-running materialization for
  -- the same instance shouldn't produce duplicate junction rows.
  UNIQUE (sop_instance_id, sop_template_task_id)
);

CREATE INDEX IF NOT EXISTS sop_generated_tasks_instance_idx
  ON sop_generated_tasks(sop_instance_id);
CREATE INDEX IF NOT EXISTS sop_generated_tasks_template_idx
  ON sop_generated_tasks(sop_template_task_id);
CREATE INDEX IF NOT EXISTS sop_generated_tasks_review_idx
  ON sop_generated_tasks(review_item_id) WHERE review_item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS sop_generated_tasks_task_idx
  ON sop_generated_tasks(task_id) WHERE task_id IS NOT NULL;

-- =========================================================================
-- updated_at triggers (use the function from 0001)
-- =========================================================================

DROP TRIGGER IF EXISTS trg_sops_updated_at ON sops;
CREATE TRIGGER trg_sops_updated_at BEFORE UPDATE ON sops
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_sop_versions_updated_at ON sop_versions;
CREATE TRIGGER trg_sop_versions_updated_at BEFORE UPDATE ON sop_versions
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_sop_template_tasks_updated_at ON sop_template_tasks;
CREATE TRIGGER trg_sop_template_tasks_updated_at BEFORE UPDATE ON sop_template_tasks
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_anchor_events_updated_at ON anchor_events;
CREATE TRIGGER trg_anchor_events_updated_at BEFORE UPDATE ON anchor_events
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_sop_generated_tasks_updated_at ON sop_generated_tasks;
CREATE TRIGGER trg_sop_generated_tasks_updated_at BEFORE UPDATE ON sop_generated_tasks
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =========================================================================
-- Runtime grants (read-only this commit)
-- =========================================================================
-- Mirrors the chunk-2 pattern: schema lands first with SELECT-only
-- grants; per-table INSERT/UPDATE granted later when behavior endpoints
-- need them. This keeps the deploy boring: applying 0009 cannot break
-- the running runtime, only widen what it can read.

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    GRANT SELECT ON sops TO opsmemory_app;
    GRANT SELECT ON sop_versions TO opsmemory_app;
    GRANT SELECT ON sop_template_tasks TO opsmemory_app;
    GRANT SELECT ON anchor_events TO opsmemory_app;
    GRANT SELECT ON sop_instances TO opsmemory_app;
    GRANT SELECT ON sop_generated_tasks TO opsmemory_app;
  END IF;
END $$;
