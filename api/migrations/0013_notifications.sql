-- OpsMemory — Migration 0013: Notification prefs + deliveries + web push subscriptions.
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Per Codex chunk-9-close STEP 10 PLAN: separate Web Push subscriptions
-- from prefs (subscriptions are per browser/device; prefs are per user
-- per channel). notification_lifecycle_state ENUM already exists from
-- 0001_initial; reuse it.
--
-- Tables added:
--   notification_prefs          per (user_id, channel) with schedule +
--                               settings jsonb. Drives the digest builder.
--   web_push_subscriptions      per browser/device. Endpoint + keys
--                               returned by PushSubscription.toJSON()
--                               from the PWA. UNIQUE on endpoint so a
--                               re-register on the same device updates
--                               in place.
--   notification_deliveries     audit log: every send / failure /
--                               suppression with idempotency_key for
--                               replay safety.

-- =========================================================================
-- notification_prefs
-- =========================================================================

CREATE TABLE IF NOT EXISTS notification_prefs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,

  -- Delivery channel. 'email_digest' reserved for a future commit if
  -- Web Push + Slack DM aren't enough.
  channel text NOT NULL,

  -- User-controlled toggle. Schedule + settings can be authored even
  -- when enabled=false so the operator can stage prefs in advance.
  enabled boolean NOT NULL DEFAULT false,

  -- Schedule jsonb shape examples:
  --   {"kind": "daily", "hour": 7, "minute": 0, "timezone": "America/Phoenix"}
  --   {"kind": "weekly", "weekday": "mon", "hour": 8, "minute": 0,
  --    "timezone": "America/Phoenix"}
  --   {"kind": "on_event", "triggers": ["task_assigned", "task_due_today"]}
  -- Application validates the kind/fields; schema only checks
  -- structural validity.
  schedule jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- Channel-specific settings:
  --   web_push: {"include_stale": true, "include_completed": false,
  --              "stale_days": 7}
  --   slack_dm: {"send_via": "n8n", "include_stale": true}
  settings jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  -- Channel allowlist. Add new channels here when they ship.
  CHECK (channel IN ('web_push', 'slack_dm', 'email_digest')),
  CHECK (jsonb_typeof(schedule) = 'object'),
  CHECK (jsonb_typeof(settings) = 'object'),
  UNIQUE (user_id, channel)
);

CREATE INDEX IF NOT EXISTS notification_prefs_user_idx
  ON notification_prefs(user_id);

CREATE INDEX IF NOT EXISTS notification_prefs_enabled_idx
  ON notification_prefs(enabled, channel) WHERE enabled = true;

DROP TRIGGER IF EXISTS trg_notification_prefs_updated_at ON notification_prefs;
CREATE TRIGGER trg_notification_prefs_updated_at BEFORE UPDATE ON notification_prefs
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =========================================================================
-- web_push_subscriptions
-- =========================================================================
-- Web Push subscriptions are per-browser-per-device. A user with three
-- devices has three rows. Each row stores the opaque endpoint URL
-- returned by the browser's push service plus the two keys needed to
-- encrypt the payload (per RFC 8291 + Web Push API).

CREATE TABLE IF NOT EXISTS web_push_subscriptions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,

  -- The browser's push service endpoint URL. Browsers return this
  -- from PushSubscription.endpoint. Length varies by provider; cap
  -- at 2048 conservatively (FCM endpoints are ~150 chars; Mozilla
  -- ~250). This is the natural unique key — re-registering on the
  -- same device returns the same endpoint.
  endpoint text NOT NULL,

  -- PushSubscription.getKey('p256dh') / .getKey('auth'), both
  -- base64url-encoded. p256dh is the P-256 ECDH public key; auth is
  -- a 16-byte random secret. These let the server encrypt the
  -- payload to this subscription.
  p256dh_key text NOT NULL,
  auth_key text NOT NULL,

  -- Operator-set device label so the Settings UI can render
  -- "Phone (Chrome)" / "Laptop (Safari)". Optional; the PWA can
  -- prompt for this on registration.
  device_label text,

  -- Snapshot of navigator.userAgent at registration. Useful for
  -- debugging which browsers are sending malformed subscriptions.
  user_agent text,

  status text NOT NULL DEFAULT 'active',

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz,    -- updated when a push to this
                               -- subscription succeeds (or returns
                               -- a non-410 error).

  CHECK (length(endpoint) BETWEEN 16 AND 2048),
  CHECK (length(p256dh_key) BETWEEN 16 AND 256),
  CHECK (length(auth_key) BETWEEN 16 AND 64),
  CHECK (status IN ('active', 'expired', 'revoked')),
  UNIQUE (endpoint)
);

CREATE INDEX IF NOT EXISTS web_push_subscriptions_user_idx
  ON web_push_subscriptions(user_id) WHERE status = 'active';

DROP TRIGGER IF EXISTS trg_web_push_subscriptions_updated_at ON web_push_subscriptions;
CREATE TRIGGER trg_web_push_subscriptions_updated_at BEFORE UPDATE ON web_push_subscriptions
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =========================================================================
-- notification_deliveries
-- =========================================================================
-- Append-only audit log. One row per attempted send (or suppressed
-- send). Reuses the notification_lifecycle_state ENUM from 0001:
--   'pending' (queued by digest builder)
--   'scheduled' (waiting on schedule.scheduled_for)
--   'sent'     (delivered to provider)
--   'failed'   (provider returned permanent error)
--   'cancelled' (operator stopped before send)
--   'suppressed' (rate limit / quiet hours / unsubscribed)
--
-- idempotency_key is the natural dedupe: a digest scheduler running
-- twice in the same minute should not fire two notifications.
-- Format: '{pref_id}:{scheduled_for_iso}' for scheduled digests,
-- '{pref_id}:event:{event_uuid}' for event-triggered.

CREATE TABLE IF NOT EXISTS notification_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  idempotency_key text NOT NULL UNIQUE,

  -- Soft pointer to the recipient. ON DELETE SET NULL preserves
  -- audit history if a user is hard-deleted (chunk-1 7-day window).
  user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  pref_id uuid REFERENCES notification_prefs(id) ON DELETE SET NULL,

  channel text NOT NULL,
  status notification_lifecycle_state NOT NULL DEFAULT 'pending',

  scheduled_for timestamptz NOT NULL,
  attempted_at timestamptz,
  sent_at timestamptz,
  failed_at timestamptz,

  -- Provider-specific identifier:
  --   web_push:  'firebase' | 'mozilla' | 'apple' (parsed from endpoint)
  --   slack_dm:  'opsmemory_bot' | 'n8n_bridge'
  provider text,

  -- Rendered payload that was sent (or would have been sent for
  -- suppressed). Web Push: {title, body, url, ...}. Slack:
  -- {channel, text, blocks?}.
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- Failure detail. {code, message, http_status?} for transport
  -- errors; {code: 'unsubscribed'} for 410 from web push (which
  -- triggers an UPDATE web_push_subscriptions SET status='expired').
  error jsonb NOT NULL DEFAULT '{}'::jsonb,

  request_id text,
  created_at timestamptz NOT NULL DEFAULT now(),

  CHECK (channel IN ('web_push', 'slack_dm', 'email_digest')),
  CHECK (jsonb_typeof(payload) = 'object'),
  CHECK (jsonb_typeof(error) = 'object'),
  CHECK (length(idempotency_key) BETWEEN 1 AND 256)
);

-- Hot path: scheduler picks up rows where status='pending' AND
-- scheduled_for <= now().
CREATE INDEX IF NOT EXISTS notification_deliveries_pending_idx
  ON notification_deliveries(scheduled_for)
  WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS notification_deliveries_user_idx
  ON notification_deliveries(user_id, created_at DESC)
  WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS notification_deliveries_pref_idx
  ON notification_deliveries(pref_id) WHERE pref_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS notification_deliveries_status_idx
  ON notification_deliveries(status, scheduled_for);

-- =========================================================================
-- Runtime grants (read-only this commit)
-- =========================================================================
-- Mirrors the chunk-7 / chunk-2 pattern: schema lands first with
-- SELECT-only. Per-table INSERT/UPDATE granted in the API endpoint
-- commit (chunk 10 step 2). The scheduler/sender granted later.

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    GRANT SELECT ON notification_prefs TO opsmemory_app;
    GRANT SELECT ON web_push_subscriptions TO opsmemory_app;
    GRANT SELECT ON notification_deliveries TO opsmemory_app;
  END IF;
END $$;
