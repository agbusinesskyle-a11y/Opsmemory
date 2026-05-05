# Chunk 1.5 — Backup Hardening + Auth Bootstrapping

This is the explicit landing zone for items deferred from Chunk 1 plus follow-ups Codex flagged in the Chunk 1 review. Chunk 1.5 sits between Chunk 1 (substrate) and Chunk 2 (API + read dashboard).

Goal: make the backup loop production-grade and add the operator tooling needed before write APIs land in Chunk 2-4.

## Scope

### Backup hardening

1. **GPG encryption** of dumps before they leave Spark #1.
   - Generate a backup keypair, store private key on Spark #1 + Spark #2 + offsite (paper or password manager).
   - Backup script `--encrypt` path produces `.dump.gpg`.
   - Restore-check decrypts before `pg_restore`.
   - Document key rotation procedure.

2. **Backblaze B2 offsite leg** (third copy of the 3-2-1 rule).
   - Application key with bucket-scoped permissions.
   - `rclone` upload after rsync to Spark #2.
   - Lifecycle policy: 90-day retention, never-delete-current-month.
   - Restore tool that pulls from B2 if Spark #2 unreachable.

3. **Weekly automated restore-check timer.**
   - `/etc/systemd/system/opsmemory-restore-check.timer`
   - Sunday 03:05 America/Phoenix
   - Failure posts to `BACKUP_ALERT_WEBHOOK_URL`.
   - Updates `restore_status.json` on every run; `/readyz` reads it.

4. **Backup locking** — prevent concurrent backups colliding (e.g., timer fires while a manual run is in progress).
   - `flock` on `/var/lib/opsmemory/backup/.lock`.
   - Both `backup_action_tracker.ps1` and `restore_check.ps1` acquire/release.

5. **Restore-from-anywhere tool** — currently `restore_check.ps1` only reads from local `BACKUP_ROOT`. Add `--source spark2` and `--source b2` modes that pull a dump from the requested location before restoring.

### Auth + identity bootstrap

6. **Service-account bootstrap CLI** — `scripts/bootstrap_service_account.py` that:
   - Generates a random API key
   - Derives `key_prefix` (first 16 chars) and `key_hash` (HMAC-SHA256 with `SERVICE_KEY_PEPPER`)
   - Inserts a row into `service_accounts`
   - Prints the raw key once (operator stores it)

7. **Service-account key rotation procedure** documented in `docs/`:
   - How to add a second key to a service account
   - How to deprecate an old key without breaking the consumer

### Cross-cutting

8. **CSRF / Origin enforcement** scaffolding before any write endpoint ships in Chunks 2-5:
   - Reject non-`same-origin` requests on mutation routes.
   - Verify `Origin` matches `tracker.kyleconway.ai`.
   - Cloudflare Access cookie gating may already do this; add a belt-and-suspenders middleware check.

9. **Tighten CSP** — replace `style-src 'unsafe-inline'` with nonce-based CSP.
   - Move inline styles in `index.html` to `web/styles.css`.
   - Add nonce on inline `<script>`/`<style>` if any remain.

10. **Container healthcheck includes DB ping**, not just `/healthz`.
    - Update Docker `healthcheck` test to hit `/readyz` (200 = healthy, 503 = unhealthy).

11. **`.env` validation script** — `scripts/validate_env.py` that:
    - Checks all required keys present
    - Refuses values with characters that break URL DSNs
    - Refuses `AUTH_MODE=local` if `ENVIRONMENT=production`
    - Run as a step in deploy + as a precondition for systemd services.

12. **Migration runner** — replace ad-hoc psql invocations with `scripts/migrate.py` that:
    - Reads `api/migrations/*.sql` in order
    - Applies any not in `schema_migrations`
    - Records `execution_ms` and `checksum`
    - Becomes the single deploy entry point in Chunk 2+.

## Acceptance

- All three backup copies present (local, Spark #2, B2) for at least one daily run.
- Encrypted dump round-trips through restore-check successfully.
- Weekly restore-check timer firing on schedule, status JSON updated, `/readyz` reflects it.
- One service account created via the bootstrap CLI; calling `/v1/whoami` with its key returns the service principal.
- CSP audit passes with no `'unsafe-inline'`.

## Out of scope (stays for later chunks)

- Real task tables (Chunk 2).
- LLM ingest pipeline (Chunk 3).
- Slack ingest (Chunk 6).
- Push notifications (Chunk 11).

## Estimated work

3-5 days of focused work, single-developer cadence. Backup hardening is most of it. CSP + service-account CLI + migration runner are each a half-day or less.

## Codex review gate

Before Chunk 2 begins: Codex senior-engineer review of the Chunk 1.5 work plus next-chunk plan. Same two-gate workflow.
