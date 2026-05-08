# 16 — Slack ingest n8n workflow build runbook (Phase C)

Step-by-step instructions for building the n8n workflow that
forwards Slack `app_mention` events to OpsMemory's
`/v1/ingest/slack` endpoint. Run this AFTER the Slack app has
been created (docs/15-slack-edge-adapter-contract.md sections
1-3 for the contract n8n must implement).

The workflow has 8 nodes total. Estimated build time: 30 min.

---

## Prerequisites

You should have these values from the Slack app creation:

- **Signing Secret** (40-char hex)
- **Bot User OAuth Token** (`xoxb-...`)
- **App ID** (`A0...`)

And from the OpsMemory side:

- **n8n-slack-ingest service key**
  (`opsmem_live_iMK7ayhAShAH0hp6_<secret>` — bootstrapped 2026-05-08)
- **OpsMemory API URL** on the docker network: `http://opsmemory-api:8000`

n8n is at https://auto.kyleconway.ai. Open it and log in.

---

## Step 1 — n8n credentials

In n8n: **Settings (gear icon) → Credentials → Create New**.

Create two credentials:

### 1a. Slack Signing Secret

- **Type:** Generic Credentials → "Custom Auth" or just a generic
  type that lets you store a single named value.
- **Name:** `Slack Signing Secret`
- **Field:** `value` = `<your 40-char hex signing secret>`

> If your n8n version's "Generic Credential" type doesn't expose
> the value to Code nodes via `$credentials`, alternative path:
> set `SLACK_SIGNING_SECRET=<value>` in the n8n container's env
> (docker-compose) and restart. The Code node falls back to
> `$env.SLACK_SIGNING_SECRET` automatically.

### 1b. OpsMemory Service Key

- **Type:** **Header Auth**
- **Name:** `OpsMemory Service Key (slack ingest)`
- **Header Name:** `X-OpsMemory-Service-Key`
- **Header Value:** `opsmem_live_iMK7ayhAShAH0hp6_<your-secret-half>`

This is the n8n-slack-ingest key minted today. Scope is
`ingest:write` only (Codex Phase C trim).

---

## Step 2 — New workflow

**Workflows → Create New** → name it `opsmemory-slack-ingest`.

---

## Step 3 — Webhook trigger node

Add a **Webhook** node (the trigger). Configure:

- **Authentication:** None (HMAC happens in our Code node)
- **HTTP Method:** POST
- **Path:** `slack-ingest` (the public URL becomes
  `https://auto.kyleconway.ai/webhook/slack-ingest`)
- **Response Mode:** "Using 'Respond to Webhook' node" (so we
  control the response from later nodes)
- **Options → Raw Body:** **ON** (CRITICAL — without this the
  HMAC verify will fail because the bytes won't match what
  Slack signed)
- **Options → Allowed Origins:** leave empty
- **Options → Response Code:** 200 (default)

Click **Save** to give the workflow a webhook URL. You'll
configure this URL in the Slack app's Event Subscriptions panel
in Step 9.

---

## Step 4 — HMAC Verify Code node

Add a **Code** node, name it `HMAC Verify`. Connect the Webhook
node's output to its input.

- **Mode:** "Run Once for Each Item" (default)
- **Language:** JavaScript
- **Code:** paste the contents of
  `infra/n8n/slack-ingest-hmac-verify.js` from this repo verbatim.

If you used the Slack Signing Secret credential approach in 1a,
attach that credential to this Code node:

- Click the credentials icon at the top-right of the Code node
- "Add Credentials" → select `Slack Signing Secret`

If you went the env-var route, no credential attachment needed.

The output has 3 shapes — see comment at top of the JS file:

- `{ reject: true, reason: '...' }` → next node should respond 401
- `{ reject: false, eventType: 'url_verification', challenge: '...' }` → respond 200 with challenge
- `{ reject: false, eventType: 'event_callback', body: {...} }` → continue downstream

---

## Step 5 — IF: reject?

Add an **IF** node, name it `IF reject?`. Connect HMAC Verify's
output to it.

- **Conditions:** `{{ $json.reject }}` is **true**
- **TRUE branch** → **Respond to Webhook** node:
  - Response Code: 401
  - Response Body: `{{ $json.reason }}`

The TRUE branch ends the workflow. The FALSE branch continues to
Step 6.

---

## Step 6 — IF: url_verification?

Add another **IF** node, name it `IF url_verification?`. Connect
the FALSE branch of `IF reject?` to it.

- **Conditions:** `{{ $json.eventType }}` equals `url_verification`
- **TRUE branch** → **Respond to Webhook** node:
  - Response Code: 200
  - Response Body: `{ "challenge": "{{ $json.challenge }}" }`
  - Headers: `Content-Type: application/json`

The TRUE branch handles Slack's one-time URL verification ping.
FALSE branch continues to Step 7.

---

## Step 7 — Build OpsMemory body Code node

Add a **Code** node, name it `Build OpsMemory body`. Connect the
FALSE branch of `IF url_verification?` to it.

- **Mode:** "Run Once for Each Item"
- **Language:** JavaScript
- **Code:** paste `infra/n8n/slack-ingest-build-body.js` verbatim.

Output:
- `{ skip: true, reason: '...' }` → respond 200 silent (drop)
- `{ skip: false, opsMemoryBody: {...} }` → forward to OpsMemory

---

## Step 8 — IF: skip?

Add an **IF** node, name it `IF skip?`. Connect Build OpsMemory
body's output to it.

- **Conditions:** `{{ $json.skip }}` is **true**
- **TRUE branch** → **Respond to Webhook**:
  - Response Code: 200
  - Response Body: empty (or `{}`)

(Drop silently. Slack must not retry.)

FALSE branch continues to Step 9.

---

## Step 9 — HTTP Request to OpsMemory

Add an **HTTP Request** node, name it `POST /v1/ingest/slack`.
Connect the FALSE branch of `IF skip?` to it.

Configure:

- **Method:** POST
- **URL:** `http://opsmemory-api:8000/v1/ingest/slack`
- **Authentication:** Header Auth → select
  `OpsMemory Service Key (slack ingest)` credential from Step 1b
- **Body Content Type:** JSON
- **Send Body:** ON
- **Specify Body:** "Using JSON"
- **JSON:** `{{ $json.opsMemoryBody }}`
- **Options → Timeout:** 5000 ms (5 sec — comfortable headroom
  but caps n8n's exposure to a slow OpsMemory)
- **Options → Response → Response Format:** JSON
- **Options → Continue On Fail:** ON (we want to handle 4xx/5xx
  with a 200 to Slack rather than failing the workflow)

> **Docker network note.** This URL only resolves if the n8n
> container is on the same docker network as the opsmemory-api
> container. If the request hangs or returns "ENOTFOUND
> opsmemory-api", attach the n8n container to the
> `opsmemory_default` network (or whichever name the OpsMemory
> compose project uses):
>
> ```
> docker network connect opsmemory_default n8n
> ```
>
> If the OpsMemory compose stack uses an embedded network, you
> may need to `docker network ls` first to find the actual name.

---

## Step 10 — Final Respond node

Add a final **Respond to Webhook** node, name it `Respond 200`.
Connect the HTTP Request node's output to it.

- **Response Code:** 200
- **Response Body:** empty or `{}`
- (Whether OpsMemory returned 2xx or 4xx, we still 200 Slack —
  Slack must not retry on our infra issues; OpsMemory's
  idempotency on the next event will recover.)

---

## Step 11 — Activate workflow

Click the **Activate** toggle at the top right of the workflow
canvas. The webhook URL is now live.

Get the URL from the Webhook node's "Display URL" link — it'll
be something like:
`https://auto.kyleconway.ai/webhook/slack-ingest`

---

## Step 12 — Smoke test with the HMAC fixture

Before pointing real Slack at it, validate n8n's HMAC implementation
matches the Python reference:

```bash
# On any machine with python3 + curl, against the live n8n URL:
python3 scripts/slack_hmac_fixture.py print-vectors \
  --webhook-url https://auto.kyleconway.ai/webhook/slack-ingest \
  --signing-secret <your-40-char-signing-secret> \
  > /tmp/slack-vectors.sh

cat /tmp/slack-vectors.sh
```

The output is 8 curl commands. Run each one and compare:

| Vector | Expect from n8n |
| ------ | --------------- |
| 01_valid_app_mention | 200 (n8n forwards to OpsMemory; OpsMemory returns 422 channel_not_mapped because we haven't seeded the channel yet — that's a downstream concern, not n8n's) |
| 02_stale_timestamp | 401 |
| 03_wrong_secret | 401 |
| 04_body_tampered | 401 |
| 05_missing_signature_header | 401 |
| 06_missing_timestamp_header | 401 |
| 07_malformed_signature_no_v0 | 401 |
| 08_url_verification_challenge | 200, body should echo the challenge string |

If 02-07 don't return 401, the HMAC implementation is wrong
somewhere — usually the raw-body capture or the
`timingSafeEqual` length-check. Re-read
docs/15-slack-edge-adapter-contract.md sections 2 and 7.

---

## Step 13 — Wire up real Slack events

Once the fixture passes:

1. Back in the Slack app at https://api.slack.com/apps:
   - Click your OpsMemory app → **Event Subscriptions**
   - Toggle "Enable Events" ON
   - **Request URL:** paste the n8n webhook URL
   - Slack will ping it and verify (this is vector 08); should
     show "Verified" with green check
2. Under **Subscribe to bot events**, add: `app_mention`
3. **Save Changes** (bottom right)
4. Slack will prompt to **Reinstall App** — do it; the bot
   re-authorizes with the new event subscription.

---

## Step 14 — Seed launch channels

Decide which 2 channels are launch channels (one per business).

Get the channel IDs:
- In Slack desktop, right-click the channel name → "View channel
  details" → at the bottom, the channel ID is shown (`C0...`).

Get the team_id:
- In any Slack URL while logged in, the workspace ID is in the
  URL path or via app credentials.

Add the bot to each channel:
- In each channel, type `/invite @OpsMemory` and confirm.

Seed the mappings on Spark:

```bash
docker exec postgres psql -U opsmemory_owner -d action_tracker -c "INSERT INTO slack_channel_mappings (team_id, channel_id, business_id, channel_name, status) VALUES ('<TEAM_ID>','<CHANNEL_ID_REDHOT>','00000000-0000-0000-0000-000000000201','#ops-redhot','active'), ('<TEAM_ID>','<CHANNEL_ID_BORDERLINE>','00000000-0000-0000-0000-000000000202','#ops-borderline','active') RETURNING id, channel_name;"
```

(Business UUIDs from the existing seeded businesses table.)

Seed Kyle's user_identity row (so mention->owner resolution works):

```bash
docker exec postgres psql -U opsmemory_owner -d action_tracker -c "INSERT INTO user_identities (user_id, provider, provider_subject, email) SELECT id, 'slack', '<TEAM_ID>:<KYLE_SLACK_USER_ID>', email FROM users WHERE email='<kyle's-opsmemory-email>' RETURNING id, provider_subject;"
```

---

## Step 15 — End-to-end smoke test

In one of the seeded channels, post:

> @OpsMemory Joanna please order more sparklers from the supplier by Friday

Within ~5-10 minutes (one worker tick + processing time), open
the PWA Review tab — a review_item should appear with the
extracted candidate. Approve it; verify a task lands in the
dashboard.

If something goes wrong, check in this order:

1. n8n execution log — did the webhook fire? Did HMAC verify
   pass?
2. `docker logs opsmemory-api` — did the POST land? What was
   the response status?
3. `SELECT * FROM ingest_events ORDER BY received_at DESC LIMIT 5`
   on Spark — did the event get inserted?
4. `SELECT * FROM llm_calls ORDER BY created_at DESC LIMIT 3` —
   did the worker pick it up and call the LLM?

---

## Notes

- **Reactions deferred.** This workflow handles `app_mention`
  only. Reaction-based ingest (emoji `:task:` triggers) is a
  Phase C v2 expansion. Document the path in section 9 of
  docs/15 for when it lands.
- **Cap counter deferred.** The Codex review recommended a
  per-channel daily cap counter in n8n's Postgres as defense in
  depth. Not built for launch — relying on OpsMemory's
  $20/day cost cap as the primary brake. Add when Slack volume
  is observed.
- **Bot user ID resolution.** The current Build OpsMemory body
  Code node strips ALL leading `<@U...>` mentions. If you want
  to preserve other-user mentions in the text (e.g. so the LLM
  sees who's being assigned), tighten the regex to strip ONLY
  the bot's own user_id once it's known.
