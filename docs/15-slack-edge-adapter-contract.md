# 15 — Slack edge adapter contract (Phase C)

The n8n workflow that bridges Slack -> OpsMemory is the most
fragile single component in Phase C. This document is the
authoritative contract n8n must implement before any real Slack
event is allowed to flow through.

Codex flagged it as the "scariest single piece" in the
2026-05-07 Phase C plan review and required this writeup plus a
fixture test BEFORE the C.5 smoke test. The companion fixture
generator is `scripts/slack_hmac_fixture.py`.

> **Trust model.** Slack signs every webhook delivery with a
> per-app secret. Once Slack's signature verifies, the bytes are
> trusted. Everything downstream of the signature check
> (channel allowlist, OpsMemory POST, n8n cap counter) runs only
> on verified payloads.

---

## 1. Wire contract Slack expects

**Endpoint shape.** Slack POSTs `application/json` bodies to a
webhook URL the operator configures in the Slack app's Event
Subscriptions panel. URL: `https://auto.kyleconway.ai/webhook/<n8n-uuid>`.

**Headers Slack always sends.**

| Header                          | Use                                    |
| ------------------------------- | -------------------------------------- |
| `X-Slack-Signature`             | `v0=<hex>` HMAC, the signature itself. |
| `X-Slack-Request-Timestamp`     | Unix-seconds when Slack sent it.       |
| `Content-Type: application/json`| Body parse hint; n8n must NOT parse it before HMAC. |
| `User-Agent: Slackbot 1.0 (+...)` | Informational, not security-relevant. |

**Headers are case-insensitive per HTTP spec.** n8n Code node
should read via lowercase keys (`headers['x-slack-signature']`)
or use a case-insensitive lookup helper.

**Slack retries on >=400 OR no 2xx within ~3 seconds**, up to 3
attempts within 1 hour. Each retry has the same body and
timestamp; idempotency on our side is keyed by the message `ts`
inside the body, not the retry attempt.

---

## 2. The HMAC v0 signature scheme

Documented at `https://api.slack.com/authentication/verifying-requests-from-slack`
(don't rely on this URL surviving a Slack docs reorg; the
algorithm is the contract).

```
base   = "v0:" + X-Slack-Request-Timestamp + ":" + RAW_BODY_BYTES
sig    = "v0=" + hex( HMAC-SHA256( signing_secret, base ) )
verify = constant_time_eq( sig, X-Slack-Signature )
```

Three properties matter:

1. `RAW_BODY_BYTES` is the EXACT bytes Slack sent. If n8n parses
   the JSON, mutates it, then re-serializes for the HMAC base,
   the canonical key ordering changes and the signature will not
   match. n8n's Webhook node must capture the raw body via the
   `Raw Body` option.
2. The constant-time compare is required. `===` or
   `Buffer.compare` would leak signature bytes via timing.
   Node's `crypto.timingSafeEqual(a, b)` works but **throws** if
   the buffers are different lengths — so length-check first or
   wrap in try/catch.
3. The signing_secret rotates only on operator action in the
   Slack admin UI. n8n credentials store one active secret. A
   future short rollover window can accept current+previous
   secrets if needed; not required for launch.

**Replay protection.** Reject if
`abs(now_unix - X-Slack-Request-Timestamp) > 300` seconds. This
is the Slack-recommended skew tolerance; do not tighten to 60s
without verifying clock sync between Slack edge and Spark host.
Tighten by syncing clocks (NTP), not by shrinking the window.

**Failure mode.** Any failure in this section -> n8n returns 401
to Slack. Slack treats 401 as terminal (no retry), but logs the
failure in the App admin UI so the operator can investigate.

---

## 3. URL verification handshake

When the operator first configures the webhook URL in Slack's
Event Subscriptions panel, Slack sends a one-time POST with body:

```json
{
  "type": "url_verification",
  "challenge": "<random 50-char-ish string>",
  "token": "<deprecated verification token, ignore>"
}
```

n8n must respond with HTTP 200 and JSON body
`{"challenge": "<the same challenge>"}` within 3 seconds. The
HMAC signature on this request is real (computed exactly the
same way), so HMAC verification runs first; do not short-circuit
the verification just because the body type is url_verification.

After this handshake succeeds Slack flips the URL to "verified"
and starts delivering real events.

---

## 4. Crypto module availability in n8n Code node

n8n's Code node runs JavaScript in a sandboxed Node.js context.
By default `require('crypto')` is available — but on
self-hosted n8n the operator can disable arbitrary `require`
calls via `NODE_FUNCTION_ALLOW_BUILTIN` and
`NODE_FUNCTION_ALLOW_EXTERNAL` env vars.

For our setup confirm one of:

- `NODE_FUNCTION_ALLOW_BUILTIN=*` is set, OR
- `NODE_FUNCTION_ALLOW_BUILTIN` includes `crypto` explicitly.

Document the value in `auto.kyleconway.ai`'s docker-compose env
when the workflow is built. If the n8n container was deployed
with strict defaults the workflow will silently fail when it
tries to compute the HMAC.

---

## 5. The 3-second Slack 2xx requirement

Slack expects 2xx within ~3 seconds. Anything slower is treated
as a failure and retried. This means **n8n must respond before
the OpsMemory POST completes** if there's any chance the
OpsMemory call is slow.

For Phase C launch volume, OpsMemory's `/v1/ingest/slack`
typically responds in <100 ms (no LLM in the request path; LLM
runs later via the worker timer). The 3-second budget is
comfortable. But if OpsMemory is slow or down, the right
behavior is:

1. n8n responds 200 to Slack immediately after HMAC verification.
2. Forward to OpsMemory as a non-blocking continuation.
3. If OpsMemory POST fails, log it in n8n. Do not let n8n's own
   retry escalate it (Slack already gives us idempotent retries
   via the message `ts` -> `source_external_id` mapping).

For launch we can use a synchronous flow (HMAC verify -> OpsMemory
POST -> 200 to Slack) since latency budget is comfortable. If
ever the LLM-side latency leaks into the ingest path
(it shouldn't, but watch for it), switch to async fork.

---

## 6. Async continuation

Even with the synchronous-for-now flow, two pieces must be
wall-clock-independent of Slack's 3-second window:

- The OpsMemory POST. If `opsmemory-api` is slow, n8n should
  still 200 Slack on time. Add a 2-second timeout on the
  OpsMemory HTTP call; on timeout, log and 200 Slack. Slack
  retries with the same `ts`; the source_external_id dedup on
  OpsMemory side handles the duplicate forward gracefully (no
  duplicate `ingest_events` row, returns `deduped: true`).
- The cap-counter increment in n8n's Postgres. Should be one
  upsert; do not block on a slow Postgres connection.

---

## 7. Failure modes — what to return when

| Trigger                                         | n8n responds  | Notes |
| ----------------------------------------------- | ------------- | ----- |
| Missing `X-Slack-Signature` header              | 401           | Slack treats as terminal; rotates op-side bug. |
| Missing `X-Slack-Request-Timestamp` header      | 401           | Same.                                          |
| Timestamp skew > 300 s                          | 401           | Replay or clock drift; investigate clock sync. |
| Signature length mismatch (truncated)           | 401           | Don't call `timingSafeEqual` with unequal lens. |
| Signature mismatch (wrong secret OR body tampered) | 401         | Real bad-actor signal; alert.                  |
| `url_verification` challenge OK                 | 200 + challenge | Setup handshake.                              |
| Event from unmapped channel (n8n allowlist miss) | 200          | Slack must not retry; we just drop silently.  |
| OpsMemory POST returns 4xx (channel_not_mapped, channel_paused, etc.) | 200 | Slack must not retry; OpsMemory rejection is final. Log it. |
| OpsMemory POST returns 5xx                      | 200           | Slack must not retry on our infra blip; OpsMemory's idempotency on next event will recover. |
| Cap counter exceeded for this channel today     | 200           | Drop silently, log.                            |
| n8n Code node throws unexpectedly               | 500           | Slack will retry, hopefully transient.         |

The pattern: **only return non-2xx for HMAC verification
failures.** Everything else returns 2xx so Slack stops retrying;
the failure is recorded server-side for the operator.

---

## 8. The OpsMemory POST shape

After HMAC verifies and the channel allowlist passes:

```
POST http://opsmemory-api:8000/v1/ingest/slack
Headers:
  X-OpsMemory-Service-Key: opsmem_live_iMK7ayhAShAH0hp6_<secret>
  Content-Type: application/json

Body:
{
  "team_id": "T01XXXX",
  "channel_id": "C01YYYY",
  "ts": "1746640000.000200",
  "text": "<message text, with bot mention stripped>",
  "user_id": "U01ZZZZ",
  "thread_ts": null,
  "channel_name": "ops-redhot",
  "user_name": "kyle",
  "team_domain": "kyleconway",
  "workspace_name": "Kyle Conway",
  "extra": {
    "slack_event_id": "Ev01XXXX",
    "slack_event_type": "app_mention",
    "original_text": "<text with the mention not stripped>"
  }
}
```

The bot mention should be stripped from `text` so the LLM
extract step doesn't waste tokens on `<@OPSMEMORY>` tokens that
aren't task-relevant. Original text preserved in `extra.original_text`
for audit reproduction.

---

## 9. Test fixture

`scripts/slack_hmac_fixture.py` produces signed Slack-shaped
requests for these test vectors:

1. Valid signature, current timestamp, app_mention body -> expect 2xx.
2. Valid signature but timestamp 6 minutes old -> expect 401 (replay).
3. Valid timestamp but signature signed with WRONG secret -> expect 401.
4. Valid timestamp + secret but body bytes mutated post-sig -> expect 401.
5. Missing X-Slack-Signature header -> expect 401.
6. Missing X-Slack-Request-Timestamp header -> expect 401.
7. Malformed signature (no `v0=` prefix) -> expect 401.
8. url_verification challenge with valid signature -> expect 200 echoing challenge.

Run the fixture against the deployed n8n workflow URL once it
exists. Each test vector outputs a curl command line; the
operator runs them and compares the response against the
expected behavior column.

The fixture also includes a standalone Python verifier function
(`verify_slack_signature`) that demonstrates the correct
algorithm. This function is the reference implementation; the
n8n Code node JavaScript should match its semantics exactly.
