-- OpsMemory — Migration 0003: Ingest events + review queue + LLM call audit.
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the txn.
--
-- Adds the ingest pipeline's persistent surface per Codex's Chunk 3 plan:
--
--   ingest_events       Raw input from any source (meeting recap, Slack,
--                       email, Excel drop, web paste-box). Idempotency
--                       keyed by content hash + per-source external id.
--                       Lifecycle: received -> extracting -> pending_review
--                       -> completed (or failed/cancelled).
--
--   review_items        One proposed mutation per candidate output of
--                       the reconciliation pipeline. Carries the target
--                       task, proposed patch, retrieved-candidates audit,
--                       confidence, AND the base task/field versions —
--                       Chunk 4 uses those for transactional conflict
--                       recheck at approval time.
--
--   auto_merge_policy   Per (source_type, action_type) gate. Seeded OFF
--                       for all meeting_recap actions per the chunk1.5
--                       phased rollout (Days 1-30 = everything to review).
--
--   llm_calls           Per-call audit: provider, model, prompt template
--                       version + hash, response, tokens, cost, latency.
--                       Required by the design's prompt-injection defense
--                       (every call traceable).
--
-- Also adds back-FKs: tasks.source_event_id, task_field_versions.source_event_id,
-- task_history.source_event_id all reference ingest_events(id) ON DELETE SET NULL.
--
-- Composite index on tasks (Codex chunk-2-close follow-up): the /v1/tasks
-- list endpoint filters on status and orders by last_activity_at DESC.

-- =========================================================================
-- ingest_events
-- =========================================================================

CREATE TABLE IF NOT EXISTS ingest_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  source text NOT NULL,            -- e.g. 'meeting_recap', 'slack_channel', 'slack_command', 'email', 'excel_drop', 'sop_file', 'web_paste'
  source_external_id text,         -- e.g. Slack message ts, email Message-ID; NULL for inline pastes

  raw_content text NOT NULL,       -- canonicalized input bytes (UTF-8 text)
  normalized_hash text NOT NULL,   -- sha256 hex of canonicalized raw_content; primary dedup key
  source_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

  status ingest_lifecycle_state NOT NULL DEFAULT 'received',

  received_at timestamptz NOT NULL DEFAULT now(),
  processing_started_at timestamptz,
  processed_at timestamptz,
  failed_at timestamptz,
  retry_count integer NOT NULL DEFAULT 0,
  error text,

  -- Cross-table audit correlation. Same value on the API request that
  -- accepted this event; lets the operator trace one customer paste
  -- across the API log + LLM call rows + review item rows.
  request_id text,

  -- Actor that submitted the event.
  actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  actor_service_account_id uuid REFERENCES service_accounts(id) ON DELETE SET NULL,
  actor_type text NOT NULL,        -- 'user' | 'service' | 'system'

  -- Pipeline provenance (set during extract/choose, NULL on insert).
  parser_version text,
  llm_model text,
  prompt_version text,

  CHECK (length(source) > 0 AND length(source) <= 64),
  CHECK (length(normalized_hash) = 64),
  CHECK (jsonb_typeof(source_metadata) = 'object'),
  CHECK (retry_count >= 0),
  CHECK (actor_type IN ('user', 'service', 'system')),
  CHECK (
    (actor_type = 'user' AND actor_user_id IS NOT NULL AND actor_service_account_id IS NULL)
    OR (actor_type = 'service' AND actor_user_id IS NULL AND actor_service_account_id IS NOT NULL)
    OR (actor_type = 'system' AND actor_user_id IS NULL AND actor_service_account_id IS NULL)
  )
);

-- Dedup: same canonicalized content can only be ingested once globally.
CREATE UNIQUE INDEX IF NOT EXISTS ingest_events_hash_uidx
  ON ingest_events(normalized_hash);

-- Per-source dedup: Slack message ts, email Message-ID, etc.
CREATE UNIQUE INDEX IF NOT EXISTS ingest_events_source_external_uidx
  ON ingest_events(source, source_external_id)
  WHERE source_external_id IS NOT NULL;

-- Pipeline workers find pending events by status.
CREATE INDEX IF NOT EXISTS ingest_events_status_idx
  ON ingest_events(status, received_at)
  WHERE status IN ('received', 'extracting', 'failed');

-- Per-source recency for monitoring.
CREATE INDEX IF NOT EXISTS ingest_events_source_received_idx
  ON ingest_events(source, received_at DESC);

DROP TRIGGER IF EXISTS trg_ingest_events_status_change ON ingest_events;
-- updated_at trigger from 0001 — we don't have updated_at on ingest_events,
-- so no trigger needed. Status transitions logged separately.

-- =========================================================================
-- review_items
-- =========================================================================

CREATE TABLE IF NOT EXISTS review_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  ingest_event_id uuid NOT NULL REFERENCES ingest_events(id) ON DELETE CASCADE,

  -- The reconciliation engine's chosen action for this candidate.
  proposed_action text NOT NULL,    -- 'CREATE_TASK' | 'UPDATE_TASK' | 'COMPLETE_TASK' | 'IGNORE' | 'AMBIGUOUS'
  target_task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,

  -- The proposed mutation as a JSON patch (chunk 4 applies it transactionally).
  proposed_patch jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- Audit context: extracted facts and which candidates the retriever surfaced.
  candidate_facts jsonb NOT NULL DEFAULT '{}'::jsonb,
  retrieved_candidates jsonb NOT NULL DEFAULT '[]'::jsonb,

  -- LLM-decided confidence in the chosen action (0..1).
  confidence numeric NOT NULL DEFAULT 0,

  -- LLM rationale text (free-form).
  reason text,

  -- 3-layer concurrency snapshot: chunk 4 will re-check these inside the
  -- apply transaction. If the task's version moved while the review was
  -- pending, the apply transactionally aborts.
  base_task_version integer,
  base_field_versions jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- Validation step output (step 5 of the 7-step pipeline). Empty when
  -- everything passes. Used by the review UI to surface "this proposal
  -- would violate constraint X".
  validation_errors jsonb NOT NULL DEFAULT '[]'::jsonb,

  status review_lifecycle_state NOT NULL DEFAULT 'pending',

  reviewer_id uuid REFERENCES users(id) ON DELETE SET NULL,
  reviewed_at timestamptz,
  applied_at timestamptz,
  rejection_reason text,

  request_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CHECK (proposed_action IN ('CREATE_TASK', 'UPDATE_TASK', 'COMPLETE_TASK', 'IGNORE', 'AMBIGUOUS')),
  CHECK (confidence >= 0 AND confidence <= 1),
  CHECK (jsonb_typeof(proposed_patch) = 'object'),
  CHECK (jsonb_typeof(candidate_facts) = 'object'),
  CHECK (jsonb_typeof(retrieved_candidates) = 'array'),
  CHECK (jsonb_typeof(base_field_versions) = 'object'),
  CHECK (jsonb_typeof(validation_errors) = 'array'),
  CHECK (
    -- CREATE_TASK proposals must NOT have a target (we're making a new one)
    (proposed_action = 'CREATE_TASK' AND target_task_id IS NULL)
    OR proposed_action <> 'CREATE_TASK'
  )
);

CREATE INDEX IF NOT EXISTS review_items_event_idx
  ON review_items(ingest_event_id);

CREATE INDEX IF NOT EXISTS review_items_status_idx
  ON review_items(status, created_at DESC);

CREATE INDEX IF NOT EXISTS review_items_target_idx
  ON review_items(target_task_id)
  WHERE target_task_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_review_items_updated_at ON review_items;
CREATE TRIGGER trg_review_items_updated_at
BEFORE UPDATE ON review_items
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =========================================================================
-- auto_merge_policy
-- =========================================================================

CREATE TABLE IF NOT EXISTS auto_merge_policy (
  source_type text NOT NULL,
  action_type text NOT NULL,
  enabled boolean NOT NULL DEFAULT false,
  -- Confidence threshold for auto-merge. Day-1 default high; ratchets
  -- down as approval rate proves out (chunk1.5 phased-rollout design).
  min_confidence numeric NOT NULL DEFAULT 0.95,
  -- Free-form note (e.g., "promoted from manual review on 2026-06-01 after 50 manual approvals at >=92% rate").
  note text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
  PRIMARY KEY (source_type, action_type),
  CHECK (action_type IN ('CREATE_TASK', 'UPDATE_TASK', 'COMPLETE_TASK')),
  CHECK (min_confidence >= 0 AND min_confidence <= 1)
);

DROP TRIGGER IF EXISTS trg_auto_merge_policy_updated_at ON auto_merge_policy;
CREATE TRIGGER trg_auto_merge_policy_updated_at
BEFORE UPDATE ON auto_merge_policy
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Seed: meeting_recap policies all OFF for chunk-3 launch. Phased rollout
-- (Days 1-30 = everything to review) per chunk1.5 design.
INSERT INTO auto_merge_policy (source_type, action_type, enabled, note)
VALUES
  ('meeting_recap', 'CREATE_TASK',  false, 'Phased rollout — Day 0. Promote after 30 days + >=90% approval rate.'),
  ('meeting_recap', 'UPDATE_TASK',  false, 'Phased rollout — Day 0. Promote later than CREATE.'),
  ('meeting_recap', 'COMPLETE_TASK', false, 'Phased rollout — Day 0. Always-review for the foreseeable future.')
ON CONFLICT (source_type, action_type) DO NOTHING;

-- =========================================================================
-- llm_calls
-- =========================================================================

CREATE TABLE IF NOT EXISTS llm_calls (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  ingest_event_id uuid REFERENCES ingest_events(id) ON DELETE CASCADE,
  review_item_id uuid REFERENCES review_items(id) ON DELETE SET NULL,

  -- Which step in the 7-step pipeline. Chunk 3 only writes 'extract' and 'choose'.
  step text NOT NULL,
  provider text NOT NULL,
  model text NOT NULL,

  prompt_template text NOT NULL,   -- e.g., 'meeting_recap_extract.v1'
  prompt_hash text NOT NULL,       -- sha256 of full prompt body
  request_body jsonb,              -- sanitized request (no raw user content unless safe)
  response jsonb,

  input_tokens integer,
  output_tokens integer,
  cost_usd numeric(10, 6),
  latency_ms integer,

  status text NOT NULL DEFAULT 'pending',
  error text,

  request_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,

  CHECK (step IN ('extract', 'choose', 'embed', 'summarize')),
  CHECK (status IN ('pending', 'success', 'failed', 'timeout', 'rate_limited')),
  CHECK (length(prompt_hash) = 64),
  CHECK (input_tokens IS NULL OR input_tokens >= 0),
  CHECK (output_tokens IS NULL OR output_tokens >= 0),
  CHECK (cost_usd IS NULL OR cost_usd >= 0),
  CHECK (latency_ms IS NULL OR latency_ms >= 0)
);

CREATE INDEX IF NOT EXISTS llm_calls_event_idx ON llm_calls(ingest_event_id);
CREATE INDEX IF NOT EXISTS llm_calls_review_idx ON llm_calls(review_item_id) WHERE review_item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS llm_calls_created_idx ON llm_calls(created_at DESC);
CREATE INDEX IF NOT EXISTS llm_calls_status_idx ON llm_calls(status, created_at DESC);

-- =========================================================================
-- Back-FKs from earlier-migration tables to ingest_events.
-- All ON DELETE SET NULL — we never want a cascade from ingest_events to
-- destroy real task data (the source event is provenance, not parentage).
-- =========================================================================

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE table_name = 'tasks'
      AND constraint_name = 'tasks_source_event_id_fkey'
  ) THEN
    ALTER TABLE tasks
    ADD CONSTRAINT tasks_source_event_id_fkey
    FOREIGN KEY (source_event_id) REFERENCES ingest_events(id) ON DELETE SET NULL;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE table_name = 'task_field_versions'
      AND constraint_name = 'task_field_versions_source_event_id_fkey'
  ) THEN
    ALTER TABLE task_field_versions
    ADD CONSTRAINT task_field_versions_source_event_id_fkey
    FOREIGN KEY (source_event_id) REFERENCES ingest_events(id) ON DELETE SET NULL;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE table_name = 'task_history'
      AND constraint_name = 'task_history_source_event_id_fkey'
  ) THEN
    ALTER TABLE task_history
    ADD CONSTRAINT task_history_source_event_id_fkey
    FOREIGN KEY (source_event_id) REFERENCES ingest_events(id) ON DELETE SET NULL;
  END IF;
END $$;

-- =========================================================================
-- Composite index for /v1/tasks hot path (Codex chunk-2-close follow-up).
-- /v1/tasks filters on status and orders by last_activity_at DESC, all
-- against deletion_state='active'. The single-column status index from
-- 0002 doesn't fully cover it.
-- =========================================================================

CREATE INDEX IF NOT EXISTS tasks_status_activity_idx
  ON tasks(status, last_activity_at DESC)
  WHERE deletion_state = 'active';

-- =========================================================================
-- Privilege hardening (per Codex chunk-1.5/2 close pattern):
-- per-table grants only, no broad ALTER DEFAULT PRIVILEGES.
-- =========================================================================

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    -- ingest_events: API inserts (POST /v1/ingest/*), pipeline updates status.
    GRANT SELECT, INSERT, UPDATE ON ingest_events TO opsmemory_app;

    -- review_items: pipeline inserts proposals; review UI updates status/reviewer.
    GRANT SELECT, INSERT, UPDATE ON review_items TO opsmemory_app;

    -- auto_merge_policy: read-only at runtime (operator updates via separate path).
    GRANT SELECT ON auto_merge_policy TO opsmemory_app;

    -- llm_calls: pipeline inserts; immutable audit (no UPDATE).
    GRANT SELECT, INSERT ON llm_calls TO opsmemory_app;
  END IF;
END $$;
