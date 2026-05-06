-- OpsMemory — Migration 0002: Tasks read model.
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Adds the core task graph:
--   - tasks                  (the canonical task object, with versioning + soft delete + supersession)
--   - task_assignees         (many-to-many users↔tasks, with role-on-task)
--   - task_businesses        (many-to-many businesses↔tasks; cross-business shared tasks first-class)
--   - task_field_versions    (per-field version vectors for 3-layer concurrency)
--   - task_history           (immutable per-field audit, fed by writes in Chunk 4-5)
--   - client_mutations       (idempotency-key ledger for offline outbox replay safety)
--
-- Adds the FK from task_state_transitions.task_id (left unconstrained
-- in 0001 because tasks didn't exist yet).
--
-- ALSO REVOKES the broad default privileges set by 0001:
--   ALTER DEFAULT PRIVILEGES IN SCHEMA public
--     GRANT SELECT, INSERT, UPDATE ON TABLES TO opsmemory_app;
-- ...and grants per-table for these new tables explicitly. Per Codex's
-- close-fix review: do not let new tables silently inherit broad
-- write defaults. Chunk 2 is read-only; writes land in Chunks 4-5
-- with their own per-table grant adjustments.
--
-- Acceptance:
--   - 6 new tables created
--   - task_state_transitions.task_id has FK to tasks(id) DEFERRABLE INITIALLY DEFERRED
--     (allows the runner's outer transaction to insert into both
--     task_state_transitions AND tasks before the FK is checked)
--   - Default privileges on future tables are now empty for opsmemory_app
--     (each Chunk-3+ table grants explicitly)
--   - Per-table grants applied for the 6 new tables: SELECT only (chunk 2 = read)

-- =========================================================================
-- New tables
-- =========================================================================

CREATE TABLE IF NOT EXISTS tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  summary text NOT NULL,
  description text,

  status task_lifecycle_state NOT NULL DEFAULT 'open',
  due_at timestamptz,

  category text,           -- free-form for now; categories table arrives later if needed
  priority text,           -- free-form ("low" / "med" / "high"); strict enum later if needed

  -- "blocked on X" — either a typed link to another task OR free-form text.
  -- Both can be set if useful (e.g. "waiting on Karen for permit, see TASK-X").
  dependency_task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,
  dependency_text text,

  -- Provenance: which ingest_event spawned this row (nullable until Chunk 3
  -- adds ingest_events; FK added in 0003 so this migration has no
  -- circular dep).
  source_event_id uuid,

  -- Completion lifecycle
  completed_at timestamptz,
  completed_by uuid REFERENCES users(id) ON DELETE SET NULL,
  completion_note text,
  reopened_at timestamptz,
  reopened_by uuid REFERENCES users(id) ON DELETE SET NULL,

  -- Stale-detection marker. Updated on any human or LLM mutation.
  last_activity_at timestamptz NOT NULL DEFAULT now(),

  -- Concurrency: version is the overall counter; field-level versions
  -- live in task_field_versions for the 3-layer concurrency check.
  version integer NOT NULL DEFAULT 1,

  -- Soft-delete + merge-supersession (chunk 1.5 design decisions)
  deletion_state deletion_lifecycle_state NOT NULL DEFAULT 'active',
  deleted_at timestamptz,
  deleted_by uuid REFERENCES users(id) ON DELETE SET NULL,
  superseded_by_task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CHECK (length(summary) > 0 AND length(summary) <= 4096),
  CHECK (
    (status = 'done' AND completed_at IS NOT NULL)
    OR (status = 'open' AND completed_at IS NULL)
  ),
  CHECK (
    (deletion_state = 'active' AND deleted_at IS NULL)
    OR (deletion_state <> 'active' AND deleted_at IS NOT NULL)
  ),
  CHECK (version >= 1)
);

CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status) WHERE deletion_state = 'active';
CREATE INDEX IF NOT EXISTS tasks_due_at_idx ON tasks(due_at) WHERE deletion_state = 'active' AND status = 'open';
CREATE INDEX IF NOT EXISTS tasks_dependency_idx ON tasks(dependency_task_id) WHERE dependency_task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS tasks_last_activity_idx ON tasks(last_activity_at);
CREATE INDEX IF NOT EXISTS tasks_deleted_at_idx ON tasks(deleted_at) WHERE deleted_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_assignees (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  -- "assignee" for the standard owner role; "watcher" for read-only follows.
  -- Plain text rather than enum so future roles don't need a migration.
  role text NOT NULL DEFAULT 'assignee',
  assigned_at timestamptz NOT NULL DEFAULT now(),
  assigned_by uuid REFERENCES users(id) ON DELETE SET NULL,
  CHECK (role IN ('assignee', 'watcher')),
  UNIQUE (task_id, user_id)
);

CREATE INDEX IF NOT EXISTS task_assignees_user_idx ON task_assignees(user_id);

CREATE TABLE IF NOT EXISTS task_businesses (
  task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE RESTRICT,
  added_at timestamptz NOT NULL DEFAULT now(),
  added_by uuid REFERENCES users(id) ON DELETE SET NULL,
  PRIMARY KEY (task_id, business_id)
);

CREATE INDEX IF NOT EXISTS task_businesses_business_idx ON task_businesses(business_id);

CREATE TABLE IF NOT EXISTS task_field_versions (
  task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  field_name text NOT NULL,
  version integer NOT NULL DEFAULT 1,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
  source_event_id uuid,
  PRIMARY KEY (task_id, field_name),
  CHECK (length(field_name) > 0 AND length(field_name) <= 64),
  CHECK (version >= 1)
);

CREATE TABLE IF NOT EXISTS task_history (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  mutation_id text,        -- client-supplied idempotency key from client_mutations.idempotency_key
  field_name text,         -- NULL for whole-task mutations (create/delete/restore)
  change_type text NOT NULL,  -- create | update | complete | reopen | soft_delete | restore | merge | etc.
  old_value jsonb,
  new_value jsonb,
  actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  actor_service_account_id uuid REFERENCES service_accounts(id) ON DELETE SET NULL,
  actor_type text NOT NULL,
  source_event_id uuid,    -- chunk 3+ ingest event link
  reason text,
  confidence numeric,      -- 0.0–1.0 LLM auto-merge confidence; NULL for human mutations
  request_id text,         -- API request id for cross-table audit correlation
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (actor_type IN ('user', 'service', 'system', 'migration', 'reconciler')),
  CHECK (
    (actor_type = 'user' AND actor_user_id IS NOT NULL AND actor_service_account_id IS NULL)
    OR (actor_type = 'service' AND actor_user_id IS NULL AND actor_service_account_id IS NOT NULL)
    OR (actor_type IN ('system', 'migration', 'reconciler') AND actor_user_id IS NULL AND actor_service_account_id IS NULL)
  ),
  CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

CREATE INDEX IF NOT EXISTS task_history_task_idx ON task_history(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS task_history_mutation_idx ON task_history(mutation_id) WHERE mutation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS client_mutations (
  idempotency_key text PRIMARY KEY,
  actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  actor_service_account_id uuid REFERENCES service_accounts(id) ON DELETE SET NULL,
  client_id text,                 -- 'pwa-sw', 'slack-bot', 'n8n-task-importer', etc.
  task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,
  base_task_version integer,      -- version the client thought it was editing
  base_field_versions jsonb,      -- per-field versions the client read; checked against current
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'received',  -- received | applied | rejected | conflict
  applied_at timestamptz,
  rejection_reason text,
  request_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (length(idempotency_key) BETWEEN 1 AND 128),
  CHECK (status IN ('received', 'applied', 'rejected', 'conflict'))
);

CREATE INDEX IF NOT EXISTS client_mutations_task_idx ON client_mutations(task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS client_mutations_actor_idx ON client_mutations(actor_user_id) WHERE actor_user_id IS NOT NULL;

-- =========================================================================
-- task_state_transitions FK to tasks(id)
-- 0001 left task_id unconstrained because tasks didn't exist yet.
-- DEFERRABLE INITIALLY DEFERRED so the migration runner's outer txn can
-- insert into tasks AND task_state_transitions before the FK is checked
-- at COMMIT.
-- =========================================================================

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE table_name = 'task_state_transitions'
      AND constraint_name = 'task_state_transitions_task_id_fkey'
  ) THEN
    ALTER TABLE task_state_transitions
    ADD CONSTRAINT task_state_transitions_task_id_fkey
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    DEFERRABLE INITIALLY DEFERRED;
  END IF;
END $$;

-- =========================================================================
-- Update updated_at trigger on tasks (uses the function from 0001)
-- =========================================================================

DROP TRIGGER IF EXISTS trg_tasks_updated_at ON tasks;
CREATE TRIGGER trg_tasks_updated_at
BEFORE UPDATE ON tasks
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =========================================================================
-- Privilege hardening (per Codex Chunk 1.5 close review):
-- 1. REVOKE the broad default privilege set by 0001 so future tables
--    don't silently inherit SELECT/INSERT/UPDATE.
-- 2. GRANT per-table SELECT-only to opsmemory_app for the 6 new tables.
--    Chunk 2 is read-only; write privileges land in Chunks 4-5 with
--    explicit per-table grants.
-- =========================================================================

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    -- Drop the broad default that was set in 0001
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      REVOKE SELECT, INSERT, UPDATE ON TABLES FROM opsmemory_app;

    -- Per-table read-only grants for the new Chunk 2 tables.
    -- Writes will be added per-table when the API needs them (Chunks 4-5).
    GRANT SELECT ON tasks TO opsmemory_app;
    GRANT SELECT ON task_assignees TO opsmemory_app;
    GRANT SELECT ON task_businesses TO opsmemory_app;
    GRANT SELECT ON task_field_versions TO opsmemory_app;
    GRANT SELECT ON task_history TO opsmemory_app;
    GRANT SELECT ON client_mutations TO opsmemory_app;
  END IF;
END $$;
