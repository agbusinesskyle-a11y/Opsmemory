# Chunk 1 Deploy Runbook

Step-by-step from "git push lands on GitHub" to "Joanna logs in from her phone." Estimated time: 60-90 minutes for a first run.

**Target**: Spark #1 (existing operational substrate, same machine running n8n + mcp-server + Postgres + cloudflared).

**Prerequisites on Spark**:
- Docker + docker-compose (already there for n8n)
- Postgres reachable on the existing `spark_internal` Docker network at `postgres:5432`
- `pg_dump`, `pg_restore`, `psql` ≥ 13 (`apt install postgresql-client-15` or similar)
- `pwsh` 7 installed (`apt install -y wget && wget https://github.com/PowerShell/PowerShell/releases/...` — see Microsoft docs)
- `rsync` and `ssh` for Spark #2 backup leg
- `cloudflared` already running and authenticated (the existing tunnel)

---

## 1. Clone the repo

```bash
sudo mkdir -p /opt/opsmemory
sudo chown $USER:$USER /opt/opsmemory
git clone https://github.com/agbusinesskyle-a11y/Opsmemory.git /opt/opsmemory
cd /opt/opsmemory
```

## 2. Create Postgres roles + database

Run as a Postgres superuser (e.g. `postgres` or your existing admin):

```bash
psql "postgres://postgres@postgres:5432/postgres" <<'SQL'
CREATE ROLE opsmemory_owner LOGIN PASSWORD '<owner-password>';
CREATE ROLE opsmemory_app   LOGIN PASSWORD '<app-password>';
CREATE DATABASE action_tracker OWNER opsmemory_owner;
GRANT CONNECT ON DATABASE action_tracker TO opsmemory_app;
SQL
```

Replace `<owner-password>` and `<app-password>` with strong, distinct passwords. Save them — they go in `.env`.

**Why two roles**: `opsmemory_owner` runs migrations (DDL); `opsmemory_app` is the runtime principal and cannot DROP TABLE. Lower blast radius if the API is compromised.

## 3. Run the migration

```bash
psql "postgres://opsmemory_owner:<owner-password>@postgres:5432/action_tracker" \
     -v ON_ERROR_STOP=1 \
     -f api/migrations/0001_initial.sql
```

You should see `BEGIN ... COMMIT` and no errors. Migration is idempotent — re-running is safe.

## 4. Verify seeds and enums

```bash
psql "postgres://opsmemory_owner:<owner-password>@postgres:5432/action_tracker" <<'SQL'
SELECT count(*) AS users        FROM users;        -- expect 4
SELECT count(*) AS businesses   FROM businesses;   -- expect 2
SELECT count(*) AS memberships  FROM business_memberships; -- expect 6
SELECT version FROM schema_migrations WHERE version='0001_initial';
SELECT typname FROM pg_type
WHERE typname IN (
  'task_lifecycle_state',
  'review_lifecycle_state',
  'ingest_lifecycle_state',
  'notification_lifecycle_state',
  'deletion_lifecycle_state'
);
SQL
```

All five enums should appear. Counts should be 4 / 2 / 6.

## 5. Verify other databases are untouched

```bash
psql "postgres://postgres@postgres:5432/postgres" -c "\l"
```

Confirm `n8n`, `openbrain`, `family_docs`, `family_health`, `litellm` databases still exist with original sizes.

## 6. Create the production `.env`

```bash
cp .env.example .env
chmod 0640 .env
```

Edit `.env` and fill:

- `CF_ACCESS_TEAM_DOMAIN` — *placeholder for now; filled in step 9*
- `CF_ACCESS_AUD` — *placeholder for now; filled in step 9*
- `SERVICE_KEY_PEPPER` — generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`
- `DATABASE_URL` — `postgresql://opsmemory_app:<app-password>@postgres:5432/action_tracker`
- `ACTION_TRACKER_DATABASE_URL` — `postgresql://opsmemory_owner:<owner-password>@postgres:5432/action_tracker`
- `RESTORE_TEST_ADMIN_URL` — `postgresql://postgres:<superuser-password>@postgres:5432/postgres`
- `BACKUP_SPARK2_TARGET` — your Spark #2 rsync target (e.g. `opsbackup@spark2:/srv/backups/opsmemory/action_tracker/`)
- `SPARK_NETWORK_NAME` — confirm matches your existing Docker network (`docker network ls`)

While the Cloudflare Access app does not exist yet, leave `AUTH_MODE=cloudflare`. The API will still start (auth dependency only fires on `/whoami`); only `/whoami` will return 401 until you finish step 9.

## 7. Start the API

```bash
cd /opt/opsmemory
docker compose up -d --build
docker compose ps
docker compose logs -f opsmemory-api
```

Watch for `opsmemory_started` and `db_pool_initialized`. Ctrl-C exits the log tail, container keeps running.

## 8. Local healthchecks

From Spark #1:

```bash
curl -s http://127.0.0.1:8010/healthz | jq
curl -s http://127.0.0.1:8010/readyz  | jq
```

Both should return `{"ok": true, ...}`. `/whoami` will 401 because there's no JWT yet — that's expected.

```bash
ss -ltnp | grep ':8010'
```

Confirm the bind address shows `127.0.0.1:8010`, NOT `0.0.0.0:8010`. If it shows `0.0.0.0`, edit compose `ports:` and restart.

## 9. Create Cloudflare Access application

In the Cloudflare dashboard:

1. **One** → **Access** → **Applications** → **Add an application** → **Self-hosted**.
2. Application name: `OpsMemory Tracker`.
3. Application domain: `tracker.kyleconway.ai`.
4. Session duration: `24 hours`.
5. Identity providers: enable **Google** (only).
6. Add policy:
   - Name: `OpsMemory Humans`
   - Action: `Allow`
   - Include: `Emails` →
     - `agbusiness.kyle@gmail.com`
     - `joanna@borderlinefireworksoutlet.com`
     - `noriega3636@gmail.com`
     - `sarahjconway@gmail.com`
7. Save.

After saving, open the application's **Overview** tab and copy:
- **Application Audience (AUD) Tag** → put in `CF_ACCESS_AUD` in `.env`
- The **team domain** (visible in your Cloudflare One settings, e.g. `https://kyleconway.cloudflareaccess.com`) → put in `CF_ACCESS_TEAM_DOMAIN`

Restart the API to pick up the new env values:

```bash
docker compose up -d
docker compose logs --tail=50 opsmemory-api
```

## 10. Add cloudflared ingress + DNS

Edit cloudflared config (typically `/etc/cloudflared/config.yml`) and add a new rule **before** the catch-all:

```yaml
ingress:
  # ... existing rules above ...
  - hostname: tracker.kyleconway.ai
    service: http://localhost:8010
  # ... existing rules below, ending with:
  - service: http_status:404
```

Add a DNS record in the Cloudflare dashboard:

```
Type: CNAME
Name: tracker
Target: b510e94c-8eab-40dd-ae8d-5c933a3896da.cfargotunnel.com
Proxy status: Proxied (orange cloud)
```

Restart cloudflared:

```bash
sudo systemctl restart cloudflared
```

## 11. Test external login

From a phone or external browser, open `https://tracker.kyleconway.ai`. You should see the Cloudflare Access login page → choose Google → sign in with your Google email → land on the OpsMemory shell rendering "Logged in as Kyle Conway — admin".

If you get **403** ("user not authorized"): the email in CF Access policy doesn't match a row in `users.email`. Check both spellings.

If you get **401** repeatedly: `CF_ACCESS_TEAM_DOMAIN` or `CF_ACCESS_AUD` is wrong, or the JWT signing key is misconfigured.

If the page never reaches Cloudflare: DNS hasn't propagated. Wait 1-5 minutes.

## 12. Run the first backup manually

```bash
sudo mkdir -p /var/backups/opsmemory/action_tracker
sudo chown $USER:$USER /var/backups/opsmemory/action_tracker
sudo mkdir -p /var/lib/opsmemory/backup
sudo chown $USER:$USER /var/lib/opsmemory/backup

# Source .env so the script picks up envs.
set -a && source /opt/opsmemory/.env && set +a

pwsh /opt/opsmemory/scripts/backup_action_tracker.ps1
```

Expected output: `pg_dump complete`, `rsync -> opsbackup@spark2:...`, `backup complete`. Check `/var/lib/opsmemory/backup/status.json` for the result row.

## 13. Run the restore check manually

```bash
set -a && source /opt/opsmemory/.env && set +a
pwsh /opt/opsmemory/scripts/restore_check.ps1
```

Expected output: `using dump ...`, `pg_restore -> action_tracker_restore_test`, `all smoke checks passed`, `restore-check complete`. Check `/var/lib/opsmemory/backup/restore_status.json`.

If smoke checks fail, the dump is bad — do not proceed. Investigate.

## 14. Enable the daily backup systemd timer

Create `/etc/systemd/system/opsmemory-backup.service`:

```ini
[Unit]
Description=OpsMemory action_tracker daily backup
After=docker.service postgresql.service

[Service]
Type=oneshot
EnvironmentFile=/opt/opsmemory/.env
ExecStart=/usr/bin/pwsh /opt/opsmemory/scripts/backup_action_tracker.ps1
User=opsmemory
StandardOutput=journal
StandardError=journal
```

Create `/etc/systemd/system/opsmemory-backup.timer`:

```ini
[Unit]
Description=OpsMemory daily backup at 02:17 America/Phoenix

[Timer]
OnCalendar=*-*-* 02:17:00 America/Phoenix
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now opsmemory-backup.timer
systemctl list-timers opsmemory-backup.timer
```

(Weekly automated restore-check timer is **deferred to Chunk 1.5**. Run `restore_check.ps1` manually for now whenever you want to verify.)

## 15. Flip readyz to require backup

After step 13 succeeds, edit `.env`:

```
READYZ_REQUIRE_BACKUP=true
```

Restart:

```bash
docker compose up -d
curl -s http://127.0.0.1:8010/readyz | jq
```

`/readyz` should still return `{"ok": true, "backup_check": "enabled"}`. If `backup_stale`, the status file is older than `READYZ_BACKUP_MAX_AGE_HOURS`.

## 16. Final smoke test (acceptance)

| Check | Command | Expected |
|---|---|---|
| API liveness | `curl -s http://127.0.0.1:8010/healthz` | `{"ok": true, "version": "chunk1", ...}` |
| API readiness | `curl -s http://127.0.0.1:8010/readyz` | `{"ok": true, "migration": "0001_initial", "backup_check": "enabled"}` |
| Loopback only | `ss -ltnp \| grep 8010` | Bind shows `127.0.0.1`, not `0.0.0.0` |
| Other DBs untouched | `psql ... "\l"` | n8n, openbrain, family_* sizes unchanged |
| External access works | `https://tracker.kyleconway.ai` from phone | CF Access challenge → Google SSO → shell |
| /whoami | View page after login | Shows "Logged in as <name> — admin" + businesses |
| PWA install | Add to Home Screen on iOS/Android | Standalone launch works, /whoami still loads |
| Backup timer | `systemctl list-timers opsmemory-backup.timer` | Next firing in `<24h` |
| Backup status | `cat /var/lib/opsmemory/backup/status.json` | `completed_at` timestamp recent |
| Restore status | `cat /var/lib/opsmemory/backup/restore_status.json` | `ok: true`, all smoke_checks present |

When all rows pass, Chunk 1 is **done**.

---

## Rollback

If something breaks badly during steps 1-13:

1. `docker compose down` (stops the API)
2. Remove the cloudflared ingress rule for `tracker.kyleconway.ai` and restart cloudflared (existing services keep working)
3. (Optional) `DROP DATABASE action_tracker; DROP ROLE opsmemory_app, opsmemory_owner;`

n8n / openbrain / family_* are isolated from this — they cannot be impacted by a Chunk 1 rollback.

## Common gotchas

- **`AUTH_MODE=cloudflare` but JWT keeps failing 401**: the team domain must be exactly `https://<team>.cloudflareaccess.com` (with `https://`, no trailing slash). The AUD must be the application AUD, not the policy AUD.
- **`/healthz` works but `/readyz` returns `db_unreachable`**: `DATABASE_URL` host or password is wrong. Check from inside the container: `docker compose exec opsmemory-api python -c "import os; print(os.environ['DATABASE_URL'])"` and try connecting from the container.
- **`PWA installs but /whoami fails`**: service worker may have cached a stale 401. In dev tools, unregister the SW and reload, or close and reopen the standalone app.
- **`pg_dump command not found`**: install `postgresql-client-15` (or matching major version) on Spark.
- **`pwsh: command not found`**: install PowerShell 7 from Microsoft's repository.
