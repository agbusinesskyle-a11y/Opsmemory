# Notifications runbook (Chunk 10)

> Operator contract for the OpsMemory notification system: per-user
> daily/weekly digests delivered via Web Push and Slack DM.

## Overview

```
                     ┌────────────────────────────────────┐
                     │  scripts/run_notification_         │
                     │  scheduler.py    (systemd timer)   │
                     └──────────────┬─────────────────────┘
                                    │ 1. walk notification_prefs
                                    │    (enabled + due)
                                    │ 2. build digest payload
                                    │ 3. claim notification_deliveries
                                    │
              ┌─────────────────────┼─────────────────────┐
              │ web_push                                  │ slack_dm
              ▼                                           ▼
   ┌──────────────────┐                        ┌────────────────────┐
   │ pywebpush        │                        │ httpx → n8n        │
   │ encrypt + POST   │                        │ webhook (auto.     │
   │ to FCM/Mozilla/  │                        │ kyleconway.ai)     │
   │ Apple push svc   │                        │ → chat.postMessage │
   └────────┬─────────┘                        └──────────┬─────────┘
            │                                             │
            │  push payload                               │  Slack DM
            ▼                                             ▼
   ┌──────────────────┐                        ┌────────────────────┐
   │ Browser SW       │                        │ User's Slack       │
   │ (web/sw.js)      │                        │ (DM channel)       │
   │ showNotification │                        │                    │
   └──────────────────┘                        └────────────────────┘
```

Per-user `notification_prefs` rows control:
- `enabled` (bool) — channel turned on
- `schedule.kind` ∈ `{daily, weekly}`, `hour`, `minute`, `timezone`,
  `weekday` (weekly only)
- `settings.include_completed` / `include_stale` / `stale_days`

## One-time setup

### 1. Generate VAPID keys (Web Push)

VAPID keys identify the OpsMemory server to push providers (Apple/
Firebase/Mozilla) per RFC 8292. Generate ONCE and store in `.env`:

```bash
python3 -c "
from py_vapid import Vapid
v = Vapid()
v.generate_keys()
print('VAPID_PUBLIC_KEY=', v.public_key)
print('VAPID_PRIVATE_KEY=', v.private_key)
"
```

Set in `.env`:

```
VAPID_PUBLIC_KEY=<88-char base64url>
VAPID_PRIVATE_KEY=<43-char base64url>
VAPID_SUBJECT=mailto:ops@kyleconway.ai
```

Restart the API. `main.lifespan` validates the three vars on boot:
- All three unset → cleanly disabled, `/v1/notifications/vapid_public`
  returns 503, the Settings UI shows "not configured".
- Partially set → API refuses to boot (fail fast on operator misconfig).
- All three set + valid → public key cached on `app.state.vapid_public_key`.

**Rotation:** generating new keys invalidates ALL existing
subscriptions. Users must click "Reconnect this browser" in the
Settings UI after rotation. Plan rotations during low-traffic windows.

### 2. n8n webhook (Slack DM)

Create an n8n workflow on auto.kyleconway.ai with these nodes:

```
[Webhook trigger: POST /webhook/opsmemory-digest, Header Auth Bearer]
    ↓
[Slack: users.lookupByEmail using {{$json.user_email}}]
    ↓
[Slack: chat.postMessage to {{$json.user.id}} with formatted blocks]
    ↓
[Respond to webhook: 200 with delivery_id echo]
```

Webhook authentication: enable Header Auth on the webhook trigger.
The shared bearer goes in OpsMemory's `.env`:

```
N8N_NOTIFICATION_WEBHOOK_URL=https://auto.kyleconway.ai/webhook/opsmemory-digest
N8N_NOTIFICATION_WEBHOOK_BEARER=<32-byte random>
```

Both vars are required when `--send` mode runs against any
`slack_dm` pref. `preflight_n8n()` exits the runner with status 1
if either is missing.

### 3. Database migrations

Apply 0013 → 0016 if not already on this build:

```bash
python3 scripts/migrate.py
```

Migrations 0013 and 0016 add `notification_prefs`,
`web_push_subscriptions`, `notification_deliveries`, and the
per-subscription `web_push_subscription_id` column.

### 4. Systemd timer for the scheduler

Install a one-shot systemd service + timer that runs every 5
minutes. Example unit (`/etc/systemd/system/opsmemory-notifications.service`):

```ini
[Unit]
Description=OpsMemory notification digest scheduler
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/opsmemory
EnvironmentFile=/opt/opsmemory/.env
ExecStart=/opt/opsmemory/.venv/bin/python scripts/run_notification_scheduler.py --send
User=opsmemory
```

Timer (`/etc/systemd/system/opsmemory-notifications.timer`):

```ini
[Unit]
Description=Run OpsMemory notification scheduler every 5 minutes

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl enable --now opsmemory-notifications.timer
```

The 5-min cadence + `DEFAULT_LOOKBACK_MINUTES=60` (configurable via
`NOTIFICATIONS_LOOKBACK_MINUTES`) means a digest fires within ~5 min
of its scheduled local time, and a missed window up to 60 min still
catches up.

## Operating

### Run modes

```bash
# Default: walk + build payload, no DB writes, no provider call.
python3 scripts/run_notification_scheduler.py

# Claim rows but don't send (rehearsal of claim contract).
python3 scripts/run_notification_scheduler.py --claim

# Ship for real (production cron uses this).
python3 scripts/run_notification_scheduler.py --send

# Force dry-run regardless of flags (kill switch).
NOTIFICATIONS_DRY_RUN=1 python3 scripts/run_notification_scheduler.py --send
```

`--send` and `--claim` are mutually exclusive (the runner exits 1
if both are set). `NOTIFICATIONS_DRY_RUN=1` always wins.

### Per-run summary line

Each invocation ends with one greppable summary:

```
[SEND] considered=12 emitted=8 delivered=14 sent=14 send_failed=0 \
       send_errors=0 skipped_channel=0 skipped_no_subs=2 \
       skipped_claim=2 skipped_unsupported=0 errors=0
```

- `considered` — prefs the walker found enabled + due
- `emitted` — prefs that produced ≥1 delivery row
- `delivered` — actual delivery rows (web_push fans out per device)
- `sent` / `send_failed` / `send_errors` — provider response classes
- `skipped_*` — see runtime classification below

### Per-row log lines (greppable)

```
[SEND] 2026-05-07T14:00:00+00:00 channel=web_push user=Kyle \
       sub=<uuid> tasks=4 total=4 key=<pref:iso:sub:id> \
       delivery_id=<uuid> send=sent http=201 code=sent title='OpsMemory: 4 tasks for you'
```

```
[SEND] 2026-05-07T14:00:00+00:00 channel=slack_dm user=Kyle \
       tasks=4 total=4 key=<pref:iso> delivery_id=<uuid> \
       send=sent http=200 code=sent title='OpsMemory: 4 tasks for you'
```

Failure classes:
- `code=sent` — 2xx
- `code=unsubscribed` — Web Push 404/410. Subscription auto-flipped
  to `status='expired'`; user re-enables via Settings → Reconnect.
- `code=ownership_changed` — subscription was reassigned to another
  user between claim and send. Audit-only; the other user's pref
  fires next cycle.
- `code=bad_request` — 4xx other than the above. Operator-actionable
  (bad payload / encoding / Slack lookup failed).
- `code=transient` — 5xx or network/timeout. No automatic retry yet;
  next scheduler tick will create a fresh row if still due in window.
- `code=config` — server misconfig (Slack: 401/403 from n8n; Web
  Push: VAPID/cert problem).

## Smoke tests

### A. Web Push end-to-end (Settings UI)

1. Log in as a user with at least one active business.
2. Settings tab → "Enable Web Push on this browser".
3. Allow the browser permission prompt.
4. Active subscriptions table shows the new row, `status=active`.
5. Click "Send test" on that row.
6. Within ~5s, an OS notification appears: "OpsMemory test push /
   If you see this, push is working."
7. Click the notification → PWA window focuses.
8. Pill on the row reads `test sent / HTTP 201`.

### B. Slack DM dry-run

```bash
python3 scripts/run_notification_scheduler.py
```

Expected: `[DRY-RUN]` lines, no Slack DM, no DB writes. If a user
has an enabled `slack_dm` pref due in window, you'll see a line for
that pref.

### C. Slack DM claim-only

```bash
python3 scripts/run_notification_scheduler.py --claim
```

Expected: `[CLAIM]` lines per due pref, `notification_deliveries`
rows inserted in `status='scheduled'`, but no n8n webhook fires.
The `--send` cron picks them up next tick — actually NO, claim mode
inserts rows the sender doesn't separately walk. Use `--send`
directly in production. `--claim` is for rehearsing claim semantics.

### D. Slack DM send

```bash
python3 scripts/run_notification_scheduler.py --send
```

Expected: `[SEND]` lines for each due pref, `notification_deliveries`
rows transition `scheduled → sent` (or `failed` with an error
JSONB), Slack DMs land in the user's DMs.

### E. Verify per-user delivery audit

```sql
SELECT id, channel, status, sent_at, failed_at,
       error->>'code' AS code, error->>'http_status' AS http_status
  FROM notification_deliveries
 WHERE user_id = '<uuid>'
   AND created_at > now() - interval '1 day'
 ORDER BY created_at DESC;
```

## Failure modes + remediation

| Symptom | Likely cause | Fix |
|---|---|---|
| API boot fails on `vapid_config_invalid` | Partial VAPID env (some vars set, others not) | Set all three or unset all three. |
| API boot fails on `tz data missing` | Missing tzdata | `apt install tzdata` or `pip install tzdata` (already in requirements.txt). |
| Runner exits 1 `sender preflight failed` | VAPID env unset OR pywebpush missing | Set VAPID env; `pip install -r api/requirements.txt`. |
| Runner exits 1 `slack sender preflight failed` | `N8N_NOTIFICATION_WEBHOOK_URL` unset | Set it (see § One-time setup #2). |
| Many `code=unsubscribed` in audit log | Browsers expired subscriptions (expected after long inactivity) | Users re-enable via Settings → Reconnect. |
| Many `code=transient` | Push provider 5xx OR network blip | Wait for next tick; if persistent, check provider status. |
| Slack DM `code=config` | n8n bearer wrong / CF Access misconfigured | Re-mint the bearer; verify CF Access policy. |
| Slack DM `code=bad_request` from n8n | `users.lookupByEmail` returned `users_not_found` | Operator must invite the user to the Slack workspace, OR remove their `slack_dm` pref. |
| Digests duplicating | Two scheduler instances racing | The `idempotency_key` UNIQUE constraint serializes; the loser logs `[CLAIM-SKIP] reason=already_claimed`. No duplicate sends. |
| User missing from digests | `users.status='active'` AND `notification_prefs.enabled=true` AND business membership active? Check `collect_due_prefs` filters. | Re-enable user / pref / membership. |

## Decommissioning a user

Soft delete:
```sql
UPDATE users SET status = 'disabled' WHERE id = '<uuid>';
```

`collect_due_prefs` filters on `u.status = 'active'`, so disabled
users stop receiving digests on the next tick. Subscriptions stay
in the table for audit but never fire.

## Decommissioning the notification system

1. Stop the systemd timer:
   ```bash
   sudo systemctl disable --now opsmemory-notifications.timer
   ```
2. (Optional) Disable all prefs server-side:
   ```sql
   UPDATE notification_prefs SET enabled = false;
   ```
3. (Optional) Mark all subscriptions revoked:
   ```sql
   UPDATE web_push_subscriptions SET status = 'revoked';
   ```
4. Unset VAPID env. The API boot now treats Web Push as cleanly
   unconfigured. The PWA Settings UI hides the Enable button.
5. Unset `N8N_NOTIFICATION_WEBHOOK_URL`. Slack DM is the only thing
   that needs n8n; turning off the URL leaves the channel inert
   without code changes.

## Related code paths

- `api/app/notifications/schedule.py` — schedule contract (shared
  validator across API + scheduler)
- `api/app/notifications/digest.py` — pure payload builder
- `api/app/notifications/scheduler.py` — walker + claim
- `api/app/notifications/sender.py` — Web Push (pywebpush + RFC 8291)
- `api/app/notifications/slack_sender.py` — Slack DM (n8n bridge)
- `api/app/v1_notifications.py` — prefs/subscriptions API + test push
- `web/app.js` — Settings UI + Send-test + Enable + Reconnect flows
- `web/sw.js` — push + notificationclick handlers
- `scripts/run_notification_scheduler.py` — the cron entry point
