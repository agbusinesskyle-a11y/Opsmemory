-- OpsMemory — Migration 0004: Review apply (Chunk 4).
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Adds the columns and grants needed to apply approved review_items
-- transactionally (the missing 7th step from the Chunk 3 reconciliation
-- pipeline).
--
-- review_items additions:
--   - applied_task_id          uuid REFERENCES tasks(id) ON DELETE SET NULL
--                              For CREATE_TASK approvals: the new task's id.
--                              For UPDATE/COMPLETE: same as target_task_id at
--                              apply time (set on success regardless of action
--                              so audits don't have to special-case CREATE).
--   - apply_mutation_id        text UNIQUE
--                              Idempotency key for apply (e.g.,
--                              "review:<review_item_id>"). Lets a retry of
--                              an interrupted apply find the prior state.
--   - last_apply_error         jsonb DEFAULT '{}'
--                              On 409/422 conflict, the error detail; cleared
--                              on success. Surfaces on the review UI.
--   - edited_at, edited_by, edit_reason
--                              Audit for PATCH /v1/review/{id} edits to
--                              proposed_patch. Lands wired in step-3 of
--                              Chunk 4; column ships now so the schema is
--                              stable.
--
-- Index:
--   - actionable-queue partial index review_items(status, created_at DESC)
--     WHERE status IN ('pending', 'needs_changes')
--
-- Grants for runtime apply (opsmemory_app):
--   - INSERT, UPDATE on tasks
--   - INSERT on task_businesses
--   - INSERT on task_assignees
--   - INSERT, UPDATE on task_field_versions
--   - INSERT on task_history
--   - INSERT on task_state_transitions
--   - INSERT, UPDATE on review_items  (status transitions on approve/reject)
--
-- Acceptance:
--   - 4 new review_items columns, all nullable / defaulted (no rewrite)
--   - apply_mutation_id UNIQUE constraint exists
--   - actionable-queue partial index exists
--   - opsmemory_app can INSERT into the 6 task-graph tables and UPDATE
--     review_items

-- =========================================================================
-- review_items columns
-- =========================================================================

ALTER TABLE review_items
  ADD COLUMN IF NOT EXISTS applied_task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS apply_mutation_id text,
  ADD COLUMN IF NOT EXISTS last_apply_error jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS edited_at timestamptz,
  ADD COLUMN IF NOT EXISTS edited_by uuid REFERENCES users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS edit_reason text;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'review_items_apply_mutation_id_key'
  ) THEN
    ALTER TABLE review_items
      ADD CONSTRAINT review_items_apply_mutation_id_key UNIQUE (apply_mutation_id);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'review_items_last_apply_error_object_chk'
  ) THEN
    ALTER TABLE review_items
      ADD CONSTRAINT review_items_last_apply_error_object_chk
        CHECK (jsonb_typeof(last_apply_error) = 'object');
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS review_items_actionable_idx
  ON review_items(status, created_at DESC)
  WHERE status IN ('pending', 'needs_changes');

CREATE INDEX IF NOT EXISTS review_items_applied_task_idx
  ON review_items(applied_task_id)
  WHERE applied_task_id IS NOT NULL;

-- =========================================================================
-- Runtime grants for apply path.
-- The apply transaction inserts/updates across 6 tables; opsmemory_app
-- needs the narrowest possible grants to do it.
-- =========================================================================

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    -- Task graph writes
    GRANT INSERT, UPDATE ON tasks TO opsmemory_app;
    GRANT INSERT ON task_businesses TO opsmemory_app;
    GRANT INSERT ON task_assignees TO opsmemory_app;
    GRANT INSERT, UPDATE ON task_field_versions TO opsmemory_app;
    GRANT INSERT ON task_history TO opsmemory_app;
    GRANT INSERT ON task_state_transitions TO opsmemory_app;
    -- Review queue mutations
    GRANT UPDATE ON review_items TO opsmemory_app;
  END IF;
END $$;
