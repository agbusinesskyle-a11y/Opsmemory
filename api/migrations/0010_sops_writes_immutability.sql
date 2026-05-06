-- OpsMemory — Migration 0010: SOP writes + immutability triggers (Chunk 7 step 3 prep).
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Per Codex chunk-7-step2 STEP 3 PLAN: do NOT ship write grants
-- without the immutability triggers, because the application code
-- alone cannot guarantee "published versions are immutable" — a
-- direct UPDATE via psql, or a future endpoint that forgot the
-- guard, would silently rewrite history.
--
-- This migration:
--   1. Grants the runtime role (opsmemory_app) the writes the
--      upcoming behavior endpoints need.
--   2. Adds partial unique indexes so each SOP has at most ONE
--      draft and at most ONE published version simultaneously.
--   3. Tightens the existing sops.latest_version_id trigger to
--      require state='published' (not just same sop).
--   4. Blocks sop_template_tasks INSERT/UPDATE/DELETE when the
--      parent version is not draft.
--   5. Enforces sop_versions state-machine + content immutability
--      post-publish via a single BEFORE INSERT OR UPDATE trigger.

-- =========================================================================
-- 1. Runtime write grants
-- =========================================================================

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    GRANT INSERT, UPDATE ON sops TO opsmemory_app;
    GRANT INSERT, UPDATE ON sop_versions TO opsmemory_app;
    GRANT INSERT, UPDATE, DELETE ON sop_template_tasks TO opsmemory_app;
    GRANT INSERT, UPDATE ON anchor_events TO opsmemory_app;
    GRANT INSERT ON sop_instances TO opsmemory_app;
    GRANT INSERT, UPDATE ON sop_generated_tasks TO opsmemory_app;
  END IF;
END $$;

-- =========================================================================
-- 2. Per-SOP at-most-one-draft + at-most-one-published
-- =========================================================================
-- An SOP can have many superseded versions but at most one currently
-- editable draft and at most one currently active published version.
-- Operator workflow: edit a draft -> publish (the previous published
-- version is moved to 'superseded' inside the publish transaction)
-- -> create a new draft for the next iteration.

CREATE UNIQUE INDEX IF NOT EXISTS sop_versions_one_draft_per_sop_uidx
  ON sop_versions(sop_id) WHERE state = 'draft';

CREATE UNIQUE INDEX IF NOT EXISTS sop_versions_one_published_per_sop_uidx
  ON sop_versions(sop_id) WHERE state = 'published';

-- =========================================================================
-- 3. sops.latest_version_id must point to a PUBLISHED version of THIS sop.
-- =========================================================================
-- Replaces the chunk-7-step1 same-sop trigger with the stricter
-- "must be published" requirement. Application code only sets
-- latest_version_id inside the publish transaction, so this trigger
-- is the schema-side guarantee that no path leaves a draft as the
-- "current" version.

CREATE OR REPLACE FUNCTION sops_latest_version_belongs_check()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_state text;
  v_sop uuid;
BEGIN
  IF NEW.latest_version_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT sop_id, state::text INTO v_sop, v_state
  FROM sop_versions WHERE id = NEW.latest_version_id;
  IF v_sop IS NULL THEN
    RAISE EXCEPTION
      'sops.latest_version_id % refers to a missing version',
      NEW.latest_version_id
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF v_sop <> NEW.id THEN
    RAISE EXCEPTION
      'sops.latest_version_id % belongs to sop %, not this sop %',
      NEW.latest_version_id, v_sop, NEW.id
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF v_state <> 'published' THEN
    RAISE EXCEPTION
      'sops.latest_version_id % is in state %; must be published',
      NEW.latest_version_id, v_state
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

-- (Trigger trg_sops_latest_version_check from 0009 is already wired
-- to this function; CREATE OR REPLACE updates the function in place.)

-- =========================================================================
-- 4. sop_template_tasks: only mutable while parent version is draft.
-- =========================================================================

CREATE OR REPLACE FUNCTION sop_template_tasks_draft_only_check()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  parent_state text;
  parent_id uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN
    parent_id := OLD.sop_version_id;
  ELSE
    parent_id := NEW.sop_version_id;
  END IF;
  SELECT state::text INTO parent_state FROM sop_versions WHERE id = parent_id;
  IF parent_state IS NULL THEN
    -- Parent gone — let the FK handle it (CASCADE on sop_version_id).
    -- Should be unreachable in practice.
    RETURN COALESCE(NEW, OLD);
  END IF;
  IF parent_state <> 'draft' THEN
    RAISE EXCEPTION
      'sop_template_tasks: parent version is %; only draft is mutable',
      parent_state
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN COALESCE(NEW, OLD);
END;
$$;

DROP TRIGGER IF EXISTS trg_sop_template_tasks_draft_only ON sop_template_tasks;
CREATE TRIGGER trg_sop_template_tasks_draft_only
BEFORE INSERT OR UPDATE OR DELETE ON sop_template_tasks
FOR EACH ROW EXECUTE FUNCTION sop_template_tasks_draft_only_check();

-- =========================================================================
-- 5. sop_versions state machine + content immutability post-publish.
-- =========================================================================
-- Allowed transitions:
--   INSERT: state must be 'draft'.
--   UPDATE draft     -> draft       (content mutation while still drafting; version_no/sop_id immutable)
--   UPDATE draft     -> published   (the publish path; sets published_at/by + finalizes change_log)
--   UPDATE published -> superseded  (a newer version's publish path moves the prior published row aside)
--   UPDATE superseded-> superseded  (audit-only churn allowed via updated_at/by)
-- Anything else is rejected with check_violation.
-- Once published, version_no, sop_id, change_log, published_at,
-- published_by become immutable.

CREATE OR REPLACE FUNCTION sop_versions_immutability_check()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.state <> 'draft' THEN
      RAISE EXCEPTION
        'sop_versions: new versions must start as draft (got %)',
        NEW.state
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;

  -- Identity columns are always immutable.
  IF NEW.version_no <> OLD.version_no THEN
    RAISE EXCEPTION 'sop_versions.version_no is immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.sop_id <> OLD.sop_id THEN
    RAISE EXCEPTION 'sop_versions.sop_id is immutable'
      USING ERRCODE = 'check_violation';
  END IF;

  -- State transitions.
  IF OLD.state = 'draft' AND NEW.state = 'draft' THEN
    -- Drafting churn: allow content edits.
    RETURN NEW;
  ELSIF OLD.state = 'draft' AND NEW.state = 'published' THEN
    -- The publish transition. Application code is expected to set
    -- published_at + published_by here. Allow content edits during
    -- this single transition.
    IF NEW.published_at IS NULL THEN
      RAISE EXCEPTION
        'sop_versions publish requires published_at to be set'
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  ELSIF OLD.state = 'published' AND NEW.state = 'superseded' THEN
    -- Supersede: only state + updated_at/by may change.
    IF NEW.published_at IS DISTINCT FROM OLD.published_at THEN
      RAISE EXCEPTION
        'sop_versions.published_at immutable post-publish'
        USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.published_by IS DISTINCT FROM OLD.published_by THEN
      RAISE EXCEPTION
        'sop_versions.published_by immutable post-publish'
        USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.change_log IS DISTINCT FROM OLD.change_log THEN
      RAISE EXCEPTION
        'sop_versions.change_log immutable post-publish'
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  ELSIF OLD.state = NEW.state AND OLD.state IN ('published', 'superseded') THEN
    -- Audit-only churn. Block any content/state change.
    IF NEW.published_at IS DISTINCT FROM OLD.published_at
       OR NEW.published_by IS DISTINCT FROM OLD.published_by
       OR NEW.change_log IS DISTINCT FROM OLD.change_log THEN
      RAISE EXCEPTION
        'sop_versions: % versions are immutable except for audit updated_at/by',
        OLD.state
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;

  -- Any other transition (published->draft, superseded->anything,
  -- draft->superseded, etc.) is rejected.
  RAISE EXCEPTION
    'sop_versions: invalid state transition % -> %',
    OLD.state, NEW.state
    USING ERRCODE = 'check_violation';
END;
$$;

DROP TRIGGER IF EXISTS trg_sop_versions_immutability ON sop_versions;
CREATE TRIGGER trg_sop_versions_immutability
BEFORE INSERT OR UPDATE ON sop_versions
FOR EACH ROW EXECUTE FUNCTION sop_versions_immutability_check();
