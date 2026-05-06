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
  CHECK (jsonb_typeof(metadata) = 'object')
);

-- Per Codex chunk-7-step1 review: the previous inline
-- UNIQUE (business_id, name) didn't match the documented intent
-- ("archived replacement can coexist"). Use a partial unique index
-- scoped to active SOPs so an operator can archive 'RedHot Opening'
-- and create a new active 'RedHot Opening' for the next year.
CREATE INDEX IF NOT EXISTS sops_business_idx ON sops(business_id);
CREATE INDEX IF NOT EXISTS sops_status_idx ON sops(status);
CREATE UNIQUE INDEX IF NOT EXISTS sops_active_name_uidx
  ON sops(business_id, name) WHERE status = 'active';

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
    -- Published rows must have a publish timestamp. The actor column
    -- (published_by) is ON DELETE SET NULL, so it can NULL out when a
    -- user is hard-deleted (chunk-1 design 7-day window). Requiring
    -- it here would make user hard-delete fail with FK error.
    -- application-layer code is responsible for setting published_by
    -- on the publish path; the schema just guards the timestamp.
    (state <> 'published') OR (published_at IS NOT NULL)
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

  -- Owner role hint for materialization (e.g. 'admin', 'owner',
  -- 'redhot_lead'). Free text — SOP assignment vocabulary is not the
  -- same thing as auth role vocabulary, so no CHECK against the auth
  -- enum. NULL means "anyone can pick this up" (do not use the
  -- string 'unassigned' for that — NULL already encodes it).
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
  -- ±10 years bounds the offset to a sane range. A typoed 999999
  -- would produce a task with due_at in the year 4754; this guards
  -- against that without ruling out anchors with multi-year prep
  -- windows (e.g. 2-year permitting cycles).
  CHECK (due_offset_days IS NULL OR due_offset_days BETWEEN -3650 AND 3650),
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
    -- 'fired' state requires the timestamp. fired_by is ON DELETE SET
    -- NULL so a user hard-delete can null it without breaking the
    -- state invariant.
    (state <> 'fired') OR (fired_at IS NOT NULL)
  ),
  CHECK (
    -- 'cancelled' state requires the timestamp; same reasoning for
    -- cancelled_by.
    (state <> 'cancelled') OR (cancelled_at IS NOT NULL)
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
-- Tenant / parent integrity triggers (per Codex chunk-7-step1 review).
--
-- Plain FKs reference only the parent's primary key, which leaves four
-- holes:
--   1. sops.latest_version_id can point to another sop's version.
--   2. anchor_events.business_id / sop_id can pair business A with
--      business B's SOP.
--   3. sop_instances.anchor_event_id / sop_version_id can pair an
--      anchor with a version belonging to a different sop.
--   4. sop_generated_tasks: instance + template from different versions.
--
-- Triggers are simpler than composite FKs here because some of the
-- referenced columns participate in ON DELETE SET NULL, which composite
-- FKs handle awkwardly (NULLing the second column would violate parent
-- PK). The triggers are BEFORE INSERT/UPDATE so violations 23514-style
-- error before any data lands.
-- =========================================================================

-- 1. sops.latest_version_id must belong to THIS sop.
CREATE OR REPLACE FUNCTION sops_latest_version_belongs_check()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.latest_version_id IS NOT NULL THEN
    IF NOT EXISTS (
      SELECT 1 FROM sop_versions
      WHERE id = NEW.latest_version_id
        AND sop_id = NEW.id
    ) THEN
      RAISE EXCEPTION
        'sops.latest_version_id % does not belong to sop %',
        NEW.latest_version_id, NEW.id
        USING ERRCODE = 'foreign_key_violation';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sops_latest_version_check ON sops;
CREATE TRIGGER trg_sops_latest_version_check
BEFORE INSERT OR UPDATE OF latest_version_id ON sops
FOR EACH ROW EXECUTE FUNCTION sops_latest_version_belongs_check();

-- 2. anchor_events: business_id must equal the SOP's business_id.
CREATE OR REPLACE FUNCTION anchor_events_business_match_check()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  sop_business uuid;
BEGIN
  SELECT business_id INTO sop_business FROM sops WHERE id = NEW.sop_id;
  IF sop_business IS NULL THEN
    RAISE EXCEPTION
      'anchor_events.sop_id % refers to a missing SOP', NEW.sop_id
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF sop_business <> NEW.business_id THEN
    RAISE EXCEPTION
      'anchor_events.business_id % does not match sops.business_id % for sop %',
      NEW.business_id, sop_business, NEW.sop_id
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_anchor_events_business_match ON anchor_events;
CREATE TRIGGER trg_anchor_events_business_match
BEFORE INSERT OR UPDATE OF business_id, sop_id ON anchor_events
FOR EACH ROW EXECUTE FUNCTION anchor_events_business_match_check();

-- 3. sop_instances: anchor's sop_id must equal version's sop_id.
CREATE OR REPLACE FUNCTION sop_instances_sop_match_check()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  anchor_sop uuid;
  version_sop uuid;
BEGIN
  SELECT sop_id INTO anchor_sop  FROM anchor_events  WHERE id = NEW.anchor_event_id;
  SELECT sop_id INTO version_sop FROM sop_versions   WHERE id = NEW.sop_version_id;
  IF anchor_sop IS NULL OR version_sop IS NULL THEN
    RAISE EXCEPTION
      'sop_instances refers to missing anchor (%) or version (%)',
      NEW.anchor_event_id, NEW.sop_version_id
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF anchor_sop <> version_sop THEN
    RAISE EXCEPTION
      'sop_instances pairs anchor sop % with version sop %; mismatched',
      anchor_sop, version_sop
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sop_instances_sop_match ON sop_instances;
CREATE TRIGGER trg_sop_instances_sop_match
BEFORE INSERT OR UPDATE OF anchor_event_id, sop_version_id ON sop_instances
FOR EACH ROW EXECUTE FUNCTION sop_instances_sop_match_check();

-- 4. sop_generated_tasks: template_task's version must equal instance's version.
CREATE OR REPLACE FUNCTION sop_generated_tasks_version_match_check()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  instance_version uuid;
  template_version uuid;
BEGIN
  SELECT sop_version_id INTO instance_version FROM sop_instances
   WHERE id = NEW.sop_instance_id;
  SELECT sop_version_id INTO template_version FROM sop_template_tasks
   WHERE id = NEW.sop_template_task_id;
  IF instance_version IS NULL OR template_version IS NULL THEN
    RAISE EXCEPTION
      'sop_generated_tasks refers to missing instance (%) or template (%)',
      NEW.sop_instance_id, NEW.sop_template_task_id
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF instance_version <> template_version THEN
    RAISE EXCEPTION
      'sop_generated_tasks pairs instance version % with template from version %',
      instance_version, template_version
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sop_generated_tasks_version_match ON sop_generated_tasks;
CREATE TRIGGER trg_sop_generated_tasks_version_match
BEFORE INSERT OR UPDATE OF sop_instance_id, sop_template_task_id ON sop_generated_tasks
FOR EACH ROW EXECUTE FUNCTION sop_generated_tasks_version_match_check();

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
