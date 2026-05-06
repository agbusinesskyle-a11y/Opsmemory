-- OpsMemory — Migration 0011: SOP immutability tightening (Chunk 7 step 3 close-fix).
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Per Codex chunk-7-step3 review:
--
--   1. sop_template_tasks_draft_only_check (from 0010) only checked
--      NEW.sop_version_id on UPDATE. An UPDATE that moves a template
--      row from a published version to a draft would mutate the
--      published version's content because the trigger sees NEW
--      (the draft destination), not OLD (the published source). Fix
--      by checking BOTH OLD and NEW on UPDATE.
--
--   2. sop_versions_immutability_check (from 0010) only forbade
--      changes to published_at / published_by / change_log post-
--      publish. metadata, created_at, and created_by could still be
--      rewritten on a published row. Codex flagged this as
--      under-enforcement; tightening so all SOP version content is
--      immutable post-publish.
--
--   3. Defense in depth: BEFORE DELETE ON sop_versions blocks
--      published / superseded version deletion entirely.
--      opsmemory_app currently has no DELETE grant on sop_versions
--      (chunk-7 step 3a), so this is defense-in-depth against a
--      future admin endpoint forgetting the rule, or an
--      operator-run psql DELETE.

-- =========================================================================
-- 1. Tighten sop_template_tasks draft-only check (handle UPDATE OLD).
-- =========================================================================

CREATE OR REPLACE FUNCTION sop_template_tasks_draft_only_check()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  parent_state text;
BEGIN
  -- For UPDATE, both source and destination versions must be 'draft'.
  -- Without this, an UPDATE that re-pointed sop_version_id from a
  -- published version to a draft would mutate the published version
  -- (the trigger only sees NEW; OLD's parent state would never be
  -- consulted).
  IF TG_OP = 'UPDATE' THEN
    SELECT state::text INTO parent_state FROM sop_versions
    WHERE id = OLD.sop_version_id;
    IF parent_state IS NOT NULL AND parent_state <> 'draft' THEN
      RAISE EXCEPTION
        'sop_template_tasks: source parent version is %; only draft is mutable',
        parent_state
        USING ERRCODE = 'check_violation';
    END IF;
    SELECT state::text INTO parent_state FROM sop_versions
    WHERE id = NEW.sop_version_id;
    IF parent_state IS NOT NULL AND parent_state <> 'draft' THEN
      RAISE EXCEPTION
        'sop_template_tasks: destination parent version is %; only draft is mutable',
        parent_state
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;

  -- INSERT / DELETE: only one side to check.
  IF TG_OP = 'DELETE' THEN
    SELECT state::text INTO parent_state FROM sop_versions
    WHERE id = OLD.sop_version_id;
  ELSE
    SELECT state::text INTO parent_state FROM sop_versions
    WHERE id = NEW.sop_version_id;
  END IF;
  IF parent_state IS NULL THEN
    -- Parent gone — let the FK CASCADE / DELETE handle it.
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

-- (The trigger trg_sop_template_tasks_draft_only from 0010 keeps
-- pointing at this function; CREATE OR REPLACE updates the body.)

-- =========================================================================
-- 2. Tighten sop_versions immutability (cover metadata, created_at,
--    created_by post-publish).
-- =========================================================================

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
  -- created_at / created_by frozen at INSERT (would be a bug to
  -- ever rewrite).
  IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'sop_versions.created_at is immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.created_by IS DISTINCT FROM OLD.created_by THEN
    RAISE EXCEPTION 'sop_versions.created_by is immutable'
      USING ERRCODE = 'check_violation';
  END IF;

  -- State transitions.
  IF OLD.state = 'draft' AND NEW.state = 'draft' THEN
    -- Drafting churn: allow content edits.
    RETURN NEW;
  ELSIF OLD.state = 'draft' AND NEW.state = 'published' THEN
    -- The publish transition. Application code is expected to set
    -- published_at + published_by here. Allow content edits during
    -- this single transition (the publish endpoint can finalize
    -- change_log + metadata).
    IF NEW.published_at IS NULL THEN
      RAISE EXCEPTION
        'sop_versions publish requires published_at to be set'
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  ELSIF OLD.state = 'published' AND NEW.state = 'superseded' THEN
    -- Supersede: only state + updated_at/by may change.
    IF NEW.published_at IS DISTINCT FROM OLD.published_at THEN
      RAISE EXCEPTION 'sop_versions.published_at immutable post-publish'
        USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.published_by IS DISTINCT FROM OLD.published_by THEN
      RAISE EXCEPTION 'sop_versions.published_by immutable post-publish'
        USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.change_log IS DISTINCT FROM OLD.change_log THEN
      RAISE EXCEPTION 'sop_versions.change_log immutable post-publish'
        USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.metadata IS DISTINCT FROM OLD.metadata THEN
      RAISE EXCEPTION 'sop_versions.metadata immutable post-publish'
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  ELSIF OLD.state = NEW.state AND OLD.state IN ('published', 'superseded') THEN
    -- Audit-only churn. Block any content/state change.
    IF NEW.published_at IS DISTINCT FROM OLD.published_at
       OR NEW.published_by IS DISTINCT FROM OLD.published_by
       OR NEW.change_log IS DISTINCT FROM OLD.change_log
       OR NEW.metadata IS DISTINCT FROM OLD.metadata THEN
      RAISE EXCEPTION
        'sop_versions: % versions are immutable except for audit updated_at/by',
        OLD.state
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION
    'sop_versions: invalid state transition % -> %',
    OLD.state, NEW.state
    USING ERRCODE = 'check_violation';
END;
$$;

-- =========================================================================
-- 3. BEFORE DELETE ON sop_versions: block published / superseded deletion.
-- =========================================================================
-- Defense in depth. The runtime opsmemory_app role has no DELETE
-- grant on sop_versions today, but a future admin endpoint or an
-- operator-run psql DELETE could remove a published row, leaving
-- sop_instances pointing at a now-missing version. Block it at the
-- schema layer.

CREATE OR REPLACE FUNCTION sop_versions_no_delete_published_check()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF OLD.state IN ('published', 'superseded') THEN
    RAISE EXCEPTION
      'sop_versions: cannot DELETE rows in state % (published/superseded versions are immutable history)',
      OLD.state
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_sop_versions_no_delete_published ON sop_versions;
CREATE TRIGGER trg_sop_versions_no_delete_published
BEFORE DELETE ON sop_versions
FOR EACH ROW EXECUTE FUNCTION sop_versions_no_delete_published_check();
