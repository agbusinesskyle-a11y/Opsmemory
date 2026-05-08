# Session 2026-05-08 (continuation) — post-launch polish

Continuation of `2026-05-08-phase-c-launch.md`. Same UTC day, but
the launch session ended at ~03:40 UTC and this one resumed at
~12:30 UTC after Kyle was rested.

> **TL;DR:** Two polish items shipped (auto-assign-by-name and
> relative-date parsing + review_apply asyncpg fix), plus
> upstream sync of yesterday's Spark-only edits to the
> Conway_Motherduckdb repo. Codex sanity-checked the work
> mid-session; verdict 0.82 confidence after the date fix lands.

## What ran

### Auto-assign by extracted name (commit 715b451)

Yesterday's launch produced "Assignees: (none)" because the
LLM correctly extracted `owner_display: "Joanna Noriega"` but
nothing translated that to a `users.id`. Slack mention-based
resolution only fires on `<@U...>` syntax, which "Joanna please
order ..." doesn't contain.

Fix: in `pipeline.process_event`, after source-specific
resolvers, look up `owner_display` in the users table when
`owner_user_id` is still unset. ILIKE for case-insensitive
match against the canonical names from
`normalize.OWNER_ALIASES_DISPLAY`. Source-agnostic — also
benefits meeting_recap. Slack mentions still take priority.

Verified end-to-end on Spark: deleted existing sparklers task +
review_items, reset event to received, reran. CREATE_TASK
candidate emerged with `owner_user_id = Joanna's UUID`; on
approve, `task_assignees` row inserted; PWA showed Joanna.

### Codex post-launch check-in

Sent a status doc covering yesterday's launch + the
just-shipped auto-assign + plans for date parsing + open-brain
sync. Codex returned 5 specific findings:

1. **Two committed credentials were exposed** (Slack signing
   secret in the session handoff, MCP service key in
   debug_mcp_key.py). Kyle accepted the exposure; rotation
   skipped.
2. Auto-assign fix functionally correct; minor improvements
   suggested (record `owner_resolution_source`, deterministic
   handling of duplicate display names).
3. **Latent asyncpg bug** in `review_apply.py` would surface
   the moment date parsing started populating non-null
   `due_at` values. Same class of bug as the
   retrieve.py one yesterday. Must fix together with date
   parsing.
4. `docs/16` runbook says scope is "ingest:write only" — stale;
   correct to include `pipeline:read:all_businesses` per the
   in-launch correction.
5. n8n HTTP timeout 5000ms vs Slack's 3-second 2xx deadline:
   tighten or move to async fork.

UI/UX deep-research approach confirmed: (1) operator observation
+ (2) comparable-product audit before any wireframing. Tracked
as task #55.

Confidence: 0.82 after date fix. (0.68 was for the moment when
the leaked secrets + latent date bug were both still in flight.)

### Item A — relative-date parsing (commit 4806a82)

Three intertwined changes, must ship together per Codex:

`normalize.py`:
- Bare day-of-week regex (`Friday`, `Tuesday` etc) — was
  silently dropping these to None. Now resolves to the next
  occurrence after the message timestamp.
- All relative-date math anchored to **America/Phoenix**, not
  UTC. The existing 17:00 cutoff was being applied in UTC,
  which lands tasks at 10am Phoenix — wrong.
- Same-day cutoff for bare day-of-week: said before 5pm Phx
  → today; said after 5pm → next week. Codex specifically
  called out the cutoff to avoid midnight-rollover surprises.

`pipeline.py`:
- Pull `received_at` into the event SELECT.
- Compute `message_ts`: for slack, parse `source_metadata.ts`
  (Slack epoch); else fall back to `received_at`; else
  `datetime.now(UTC)`.
- Pass `message_ts` as `now=` to `normalize_candidates` so
  worker-tick lag doesn't skew due dates by hours.

`review_apply.py`:
- New `_coerce_due_at(value)` helper: accepts ISO string from
  jsonb or a real datetime, returns tz-aware datetime suitable
  for asyncpg's `::timestamptz` bind. Used in CREATE_TASK
  INSERT and UPDATE_TASK dynamic SET. Prevents the latent
  "expected datetime ... got str" DataError class on approve.

Self-test confirmed Codex's expected behavior:
- Anchor Fri 9am Phoenix: "Friday" → 2026-05-09T00:00 UTC
  (= same Fri 5pm Phx). "Tuesday" → 2026-05-13T00:00 UTC
  (= next Tue 5pm Phx). Matches Codex's exact prediction.
- Anchor Fri 6pm Phoenix (past 5pm cutoff): "Friday" →
  2026-05-16T00:00 UTC (= next Fri 5pm Phx).
- Unparseable string → None.

End-to-end verified: posted "@OpsMemory schedule the truck
inspection by Friday" in #ops-redhot. ingest_events landed,
worker extracted via gpt-5.5,
`due_at = 2026-05-09T00:00:00+00:00`. Operator approved.
review_apply path didn't crash. Task in dashboard with the
right due date.

### Item B — Conway_Motherduckdb upstream sync

Yesterday's Spark-only edits flowed back to the canonical local
repo. Three changes:
1. `infrastructure/spark1-postgres-redis.compose.yml`: added
   `SLACK_SIGNING_SECRET` and `NODE_FUNCTION_ALLOW_BUILTIN`
   env-var forwarding lines into the n8n service block.
2. `infrastructure/litellm/config.yaml`: new file. The litellm
   proxy had been edited in place on Spark over time and never
   committed upstream — pulled the current Spark version down
   via scp, committed as-is. 9 model_list entries (6 Claude +
   3 GPT), all API keys via os.environ indirection.
3. `infrastructure/.env`: stays Spark-only (gitignored,
   contains the actual signing secret value).

Conway_Motherduckdb commit `092181f` pushed to
`github.com/agbusinesskyle-a11y/Conway_Motherduckdb`.

## OpsMemory commits today

```
4806a82 chunk-5 polish: relative-date parsing + review_apply asyncpg fix
715b451 chunk-5 polish: source-agnostic owner_display -> user_id resolution
```

Total OpsMemory commits since chunk-13-close: **15**.

## Remaining work tracked

- **#54 polish-tail** — bundle: docs/16 stale-scope correction;
  n8n HTTP timeout reduction (5000→<3000ms or async fork);
  record `owner_resolution_source` field; deterministic
  handling of duplicate display names. All low-priority.
- **#55 UI/UX deep-research** — Codex-confirmed sequence:
  observe → audit → artifact → wireframes. Don't reorder.

## Open questions for next session

1. Should we tighten the n8n timeout now (cheap, but Slack
   retries are mostly handled by our idempotency anyway), or
   leave it until we see evidence of OpsMemory slowness
   biting us?
2. UI/UX research: who watches the operator sessions — Kyle
   self-records, or do we need an outside set of eyes?
3. The `docs/16` stale-scope note: probably worth fixing in
   the same polish-tail commit so future operators don't
   bootstrap a service account with too-narrow scopes.
