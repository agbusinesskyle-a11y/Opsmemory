# Session 2026-05-08 — Phase C Slack ingest, end-to-end live

> **TL;DR:** OpsMemory now creates tasks from real `@OpsMemory ...`
> messages in mapped Slack channels. End-to-end pipeline (Slack → n8n →
> /v1/ingest/slack → worker → review → task) is live. 13 commits, 4 prior-
> chunk bugs found + fixed, 1 net-new chunk shipped (chunk-5 activation).
> Polish items remain (auto-assign by name, "Friday"→date parsing,
> Spark↔open-brain sync) but no launch blockers.

This file is the standing handoff. Anyone (or future Claude) picking up
the project should read **this file first**, then `MEMORY.md` at the root
of `C:\Users\agbus\.claude\projects\C--Users-agbus\memory\`, then dive
into specifics.

---

## Where we started today (morning)

- All 13 chunks were "shipped" 2026-05-06 with chunk-1-close .. chunk-13-close tags.
- The PWA was up at tracker.kyleconway.ai. Push notifications worked.
  Weekly Gmail digest existed in dry-run mode.
- **Real test:** Kyle asked "if someone Slacks about a task, does it create one?"
  Honest answer: no. The pipeline's code was deployed but had never
  successfully processed a real LLM call.

Codex senior-advisor reviewed two plans during the day:
- Morning Phase A→E plan (verdict: fix-before-build, 3 hard blockers)
- Evening Phase C plan (verdict: fix-before-build, 5 specific fixes)

Both reviews are at `C:\Users\agbus\AppData\Local\Temp\codex_*.out`.

---

## What ran today (in order)

### Phase B (morning) — meeting recap end-to-end

Activated chunk-3 LLM ingest. Surfaced and fixed 4 hidden bugs from
prior chunks:

- `pipeline._record_llm_call` had 16 args → 15 placeholders. asyncpg
  silently raised inside `on_call`, so `llm_calls` audit was empty for
  every chunk-3 invocation since deploy. (commit `5975eb4`)
- `httpx` missing from `api/requirements.txt`. lazy-imported by
  `llm_client` only for non-mock providers; health checks never
  exercised it. (commit `5975eb4`)
- `prompts/__init__.py` missing on Spark (rsync'd `.md` files but
  not the Python file). `from .prompts import load_prompt` failed.
  (commit `5975eb4`, runtime fix only — local repo had it)
- `bootstrap_service_account.py` had two bugs: `psql -c` doesn't
  perform `:'name'` substitution (only stdin/-f does), and CASE WHEN
  expires_iso parse-time type-checks the ELSE branch. (commit `5975eb4`)
  Plus a false third "fix" I introduced (`key_prefix=opsmem_live_<kid>`
  vs auth.py's bare `<kid>` lookup) that bit us again in Phase C.
  Reverted in commit `c48116f`.
- LiteLLM had no GPT entries; gpt-4.1 was being served via fragile
  pass-through. Added explicit `gpt-4.1`, `gpt-5.4-mini`, `gpt-5.5`
  entries pointing at `OPENAI_API_KEY`. (open-brain repo edit, on
  Spark only, NOT yet committed upstream)

Q2 cost cap was the day's biggest piece (commit `490fdf4`):
- `llm_client._call_litellm` parses `body["usage"]` and
  `x-litellm-response-cost` header; populates `cost_usd` + token
  counts on every successful real-provider call.
- Hardcoded `PRICES_PER_MTOKEN` table for fallback (gpt-4.1, claude
  family); gpt-5.x family relies on LiteLLM's response header.
- `BudgetExceeded` / `BudgetUnknown` exceptions; `pre_check`
  callback on `run_step` fires before each model attempt. Pipeline
  catches budget exceptions and marks events `failed` with
  retry-tomorrow semantics.
- Force-tested at `INGEST_LLM_DAILY_USD_CAP=0.001` with $0.0303 spent;
  worker correctly tripped with `budget_exceeded` error.

A.6 worker timer (commit `60b6190`):
- `infra/systemd/opsmemory-worker.{service,timer}` — every 5 min,
  `--limit 10`, `RandomizedDelaySec=30s`. Installed disabled, then
  enabled after Q2 was verified live (Codex's gate).
- Companion fix: `gpt-5.x` family rejects `temperature: 0` ("only
  default 1 supported"). `llm_client` omits `temperature` for
  gpt-5.x; keeps `temperature=0` for gpt-4.1 + claude.

End of Phase B: meeting recap → tasks works end-to-end. Worker auto-
processes ingest_events every 5 min.

### Phase C plan + scaffolding (evening)

Wrote a comprehensive Phase C plan + sent to Codex. Verdict:
fix-before-build at 0.80, 5 specific fixes required. Plan + verdict
at `C:\Users\agbus\AppData\Local\Temp\codex_phase_c_*.out`.

The 5 fixes:
1. Slack `app_mention` event subscription (not `message.channels`) — done in Slack admin UI
2. **API-side channel gate** at `/v1/ingest/slack` (commit `4b3921b`) — 422 channel_not_mapped / channel_paused
3. **HMAC fixture test** (commit `aa56597`) — `scripts/slack_hmac_fixture.py` + `docs/15-slack-edge-adapter-contract.md`. 8 vectors, all pass on n8n.
4. n8n docker-network verify — both n8n and opsmemory-api are on `infrastructure_backend`. Confirmed during build.
5. Trim service-account scopes to `ingest:write` only — done at bootstrap. Later **partially reverted** (added `pipeline:read:all_businesses` back) because the worker uses the actor's scope for retrieval. n8n itself remains write-only at the surface.

Plus 2 bonus code changes:
- **auth.py docker-network change** (commit `c48116f`): drop CF JWT
  requirement on the service-key path. Internal n8n→opsmemory-api
  calls don't go through CF; the HMAC service key alone is the auth.
  External callers still gated by CF Access at the edge.
- **n8n workflow JSON generator** (commit `d1081d6`):
  `scripts/build_n8n_slack_workflow.py` emits an importable workflow
  JSON with the Code node JS embedded. Operator imports once instead
  of clicking through 11 nodes.

### Phase C build (late evening)

Walked through:
- Created Slack app at api.slack.com/apps. App ID `A0B1Z8DH171`.
  Bot OAuth scopes: `app_mentions:read`, `chat:write`, `channels:read`,
  `users:read`. Skipped `users:read.email`, `channels:history`,
  `reactions:read` per Codex trim.
- Added `SLACK_SIGNING_SECRET` to
  `/opt/open-brain/infrastructure/.env` (NOT in opsmemory repo).
  Forwarded into n8n container via the compose file
  (`scripts/add_slack_signing_secret_to_n8n.py`).
- Added `NODE_FUNCTION_ALLOW_BUILTIN: crypto` to the n8n compose env
  block — n8n Code node sandboxes built-ins by default; HMAC code
  needs `require('crypto')`.
- Created OpsMemory Service Key (slack ingest) Header Auth
  credential in n8n UI.
- Imported `infra/n8n/opsmemory-slack-ingest.workflow.json` to n8n.
  All 11 nodes, fully wired, credential auto-mapped.
- Activated the workflow.
- Ran the 8-vector fixture test against the live n8n URL: 8/8
  matched expected behavior on the second run (after first run
  found the crypto-disallowed env-var gap).
- Configured Slack Event Subscriptions URL → `https://auto.kyleconway.ai/webhook/slack-ingest`. Slack verified (real `url_verification` HMAC handshake, our fixture path validated against actual Slack).
- Subscribed bot to `app_mention` event. Saved.
- Created `#ops-redhot` and `#ops-borderline` channels. Invited the bot. Captured channel IDs.
- Seeded `slack_channel_mappings` rows for both channels.
- Posted `@OpsMemory Joanna please order more sparklers from the supplier by Friday` in `#ops-redhot`.
- First run failed: bootstrap-script regression (key_prefix wrote
  full prefix instead of bare kid). Fixed the row, scp'd corrected
  bootstrap script up.
- Second run failed: `retrieve.py` was passing `.isoformat()` strings
  to asyncpg `::timestamptz` params. Hidden because meeting_recap
  doesn't trigger that code path; slack_message does. Fixed in
  commit `a9bfc14`.
- Third run: full success. ingest → extract gpt-5.4-mini ($0.0014)
  → slack_resolve channel→redhot → choose short-circuit CREATE_TASK
  (0.85 confidence) → review_item visible in PWA.
- Kyle approved → task in dashboard.

Then we flipped extract from `gpt-5.4-mini` to `gpt-5.5` per Kyle's
preference (smarter for short messages; reserve mini for big docs).
Reprocessed the same Slack event — gpt-5.5 detected the existing task
from prior approval and proposed UPDATE_TASK at 0.88 (the CHOOSE step
working as designed) with `owner_display: "Joanna Noriega"` and
`category: "vendor"` correctly extracted.

---

## Live state on Spark

- `opsmemory-worker.timer`: enabled, fires every 5 min,
  `--limit 10`. Last tick: see `systemctl list-timers`.
- `opsmemory-notifications.timer` + `opsmemory-weekly-digest.timer`:
  also active (chunks 10/11).
- `opsmemory-slack-ingest` workflow in n8n: active.
- Slack app installed in workspace `T0A9REH8TPF`. Event Subscriptions
  configured. Bot joined `#ops-redhot` and `#ops-borderline`.
- Service accounts: `n8n-meeting-recap` (kid `cPiHRGtInVPr31gd`,
  scopes `ingest:write,pipeline:read:all_businesses`),
  `n8n-slack-ingest` (kid `iMK7ayhAShAH0hp6`,
  scopes `ingest:write,pipeline:read:all_businesses`).
- Channel mappings: 2 rows (redhot + borderline) status=active.
- `INGEST_LLM_EXTRACT_MODELS=gpt-5.5,gpt-4.1`
  `INGEST_LLM_CHOOSE_MODELS=gpt-5.5,gpt-4.1`
  `INGEST_LLM_DAILY_USD_CAP=20`

---

## Spark vs local repo divergences (sync upstream when convenient)

Files modified directly on Spark (not yet committed back to the local
open-brain repo, which is the canonical source):

- `/opt/open-brain/infrastructure/spark1-postgres-redis.compose.yml`
  - Added `SLACK_SIGNING_SECRET: ${SLACK_SIGNING_SECRET:-}`
  - Added `NODE_FUNCTION_ALLOW_BUILTIN: crypto`
  - Backup: `.bak.before-slack-secret`
- `/opt/open-brain/infrastructure/.env`
  - Added `SLACK_SIGNING_SECRET=5da811de52dd297447b04eb977249381`
- `/opt/open-brain/infrastructure/litellm/config.yaml`
  - Added `gpt-4.1`, `gpt-5.4-mini`, `gpt-5.5` model_list entries
  - (From earlier today; see morning Phase A activation work)

The opsmemory repo itself (`C:\opsmemory`) is fully up to date and
13 commits ahead of `origin/main`.

---

## Polish items / next session candidates

None of these are launch blockers; pipeline is functional today.

1. **Auto-assign by extracted name** — `owner_display: "Joanna Noriega"` is
   captured but not propagated to `task_assignees`. UPDATE_TASK patch
   builder in `pipeline.process_event` only includes `summary`,
   `due_at`, `dependency_text`. Two-part fix:
   - Extend `slack_resolve.py` (and possibly `normalize.py`) to do
     name-based lookup against `user_identities` rows seeded with name
     aliases.
   - Add `owner_user_id` to UPDATE_TASK patches when resolved (already
     done for CREATE_TASK).
2. **Relative-date parsing in `normalize.py`** — LLM extracted
   `due_hint: "Friday"`; normalize couldn't translate to absolute date.
   Either (a) update prompt to require ISO 8601 date the model
   computes itself given the message timestamp, or (b) add a
   relative-date parser to `normalize.py` (Friday → next Friday from
   message_ts).
3. **Sync open-brain edits upstream** — see "Spark vs local repo
   divergences" above. Three files to mirror back to local checkout
   then push.
4. **Q1 prompt tuning** — original Q1 was about gpt-5.4-mini's lazy
   hint extraction. gpt-5.5 fixed most of it (owner + category came
   through correctly). Remaining gap is due_at parsing (item 2 above)
   and edge-case hints. Probably not needed as a separate item now;
   merge into item 2.
5. **Reactions path** — Codex Phase C trim deferred `:task:` emoji
   reactions until threaded-message handling tested. Add when launch
   volume justifies it. Section 9 of `docs/15-slack-edge-adapter-contract.md`
   is the spec.
6. **Per-channel daily cap counter in n8n** — Codex defense-in-depth
   recommendation, deferred. Currently relying on OpsMemory's $20/day
   cost cap as the primary brake. Add when Slack volume is observed.
7. **n8n→OpsMemory async fork** — currently synchronous. If
   OpsMemory's `/v1/ingest/slack` ever becomes slow (e.g., 5xx
   storms), n8n could miss Slack's 3-second 2xx deadline. Section 6
   of `docs/15` documents the fork path.
8. **Backfill `user_identities` rows** — for now only Kyle has a
   default identity (via Cloudflare Access email). Add Joanna,
   Caleb, Sarah as their slack ids become known.

---

## Files / commits to look at first

If picking this up cold:

1. **This file.**
2. `C:\Users\agbus\.claude\projects\C--Users-agbus\memory\MEMORY.md`
   — the index. Specifically:
   - `project_opsmemory_chunk3_activation.md`
   - `project_opsmemory_phase_c_plan.md`
   - `feedback_opsmemory_auth_mode_testing.md`
3. `docs/15-slack-edge-adapter-contract.md` — the wire contract n8n
   implements. Authoritative if anything about HMAC / event handling
   is unclear.
4. `docs/16-slack-n8n-build-runbook.md` — step-by-step n8n build.
   The one-shot import path is `infra/n8n/opsmemory-slack-ingest.workflow.json`
   (commit `d1081d6`).
5. `git log --oneline d4a9a1d..HEAD` — today's 13 commits.
6. Codex review files in `%TEMP%\codex_*.out` — verdicts + reasoning
   for the major decisions today.

---

## Smoke-test recipe (validate the pipeline still works)

```bash
# 1. Confirm worker timer is firing
ssh tolson@spark
systemctl list-timers opsmemory-worker.timer

# 2. Confirm n8n workflow active
curl -i -sS https://auto.kyleconway.ai/webhook/slack-ingest \
  -H "Content-Type: application/json" -d '{}' | head -1
# expect 401 missing_signature_header

# 3. Run the 8-vector HMAC fixture
python3 /opt/opsmemory/scripts/slack_hmac_fixture.py print-vectors \
  --webhook-url https://auto.kyleconway.ai/webhook/slack-ingest \
  --signing-secret <signing-secret-from-.env> \
  > /tmp/v.sh
bash /tmp/v.sh 2>&1 | grep -oE "HTTP/2 [0-9]+|signature_[a-z_]+|missing_[a-z_]+|timestamp_skew_exceeded|challenge"
# expect 8 status codes interleaved with reason strings

# 4. Post a test message in #ops-redhot
# "@OpsMemory test task by Friday"
# Within ~5 min, check the PWA Review tab for a new candidate.
```
