// n8n Code node script — Slack HMAC v0 verify.
//
// Drop this verbatim into the FIRST Code node after the Webhook
// trigger in the slack-ingest workflow.
//
// Companion docs:
//   docs/15-slack-edge-adapter-contract.md (the contract)
//   docs/16-slack-n8n-build-runbook.md     (step-by-step n8n setup)
//   scripts/slack_hmac_fixture.py          (Python reference + tests)
//
// Prerequisites:
//   - Webhook node configured with "Raw Body" enabled. The raw bytes
//     must arrive via $binary.data.data (base64); if you parse the
//     body before HMAC, the canonical key ordering shifts and the
//     signature will not match.
//   - n8n credential 'Slack Signing Secret' (Generic Credentials
//     type, single 'value' field). Reference it via
//     $credentials.slackSigningSecret in n8n's credentials helper,
//     OR — if your version doesn't expose it that way — fall back
//     to an n8n environment variable SLACK_SIGNING_SECRET.
//
// Output (single item):
//   { reject: true, reason: '<why>' }              - n8n returns 401
//   { reject: false, eventType: 'url_verification', challenge: '...' } - 200 + challenge
//   { reject: false, eventType: 'event_callback', body: {...} } - continue downstream
//
// The output schema is the contract for the next node (the IF /
// router). Keep the field names stable.

const crypto = require('crypto');

// ---- Get signing secret from credential or env --------------------
let signingSecret = null;
try {
  // Preferred: a Generic Credentials credential in n8n.
  signingSecret = $credentials?.slackSigningSecret?.value ?? null;
} catch (e) { /* credential not bound; fall through */ }
if (!signingSecret) {
  signingSecret = $env?.SLACK_SIGNING_SECRET ?? null;
}
if (!signingSecret) {
  throw new Error(
    'Slack signing secret not found. Bind the "Slack Signing Secret" ' +
    'credential to this Code node OR set SLACK_SIGNING_SECRET in n8n env.'
  );
}

// ---- Pull headers (case-insensitive) -----------------------------
const item = $input.first();
const headers = item.json?.headers ?? {};
const h = {};
for (const k in headers) h[k.toLowerCase()] = headers[k];

const sigHeader = h['x-slack-signature'];
const tsHeader = h['x-slack-request-timestamp'];

if (!sigHeader) {
  return [{ json: { reject: true, reason: 'missing_signature_header' } }];
}
if (!tsHeader) {
  return [{ json: { reject: true, reason: 'missing_timestamp_header' } }];
}

// ---- Replay protection: 5-min skew (Slack-recommended) -----------
const now = Math.floor(Date.now() / 1000);
const ts = parseInt(tsHeader, 10);
if (Number.isNaN(ts) || Math.abs(now - ts) > 300) {
  return [{ json: { reject: true, reason: 'timestamp_skew_exceeded' } }];
}

// ---- Raw body -----------------------------------------------------
// Webhook node with "Raw Body" puts bytes here. Some n8n versions
// route them slightly differently — try the documented path first
// then fall back to alternatives we've seen in 2.x deployments.
let rawBody = null;
try {
  const b64 = item.binary?.data?.data;  // n8n 2.x default for Raw Body
  if (b64) rawBody = Buffer.from(b64, 'base64');
} catch (_e) { /* fall through */ }
if (!rawBody) {
  // Fallback: some webhook configs leave the raw string at item.json.body
  // when the request had Content-Type application/json. NOT
  // canonical — only safe if no JSON re-serialization happened
  // upstream. Use only if Raw Body binary path is unavailable.
  const bodyStr = typeof item.json?.body === 'string'
    ? item.json.body : null;
  if (bodyStr) rawBody = Buffer.from(bodyStr, 'utf8');
}
if (!rawBody) {
  return [{ json: { reject: true, reason: 'raw_body_unavailable' } }];
}

// ---- Compute expected signature ---------------------------------
const base = Buffer.concat([Buffer.from(`v0:${tsHeader}:`), rawBody]);
const expected =
  'v0=' + crypto.createHmac('sha256', signingSecret).update(base).digest('hex');

// ---- Constant-time compare (lengths must match) -----------------
if (sigHeader.length !== expected.length) {
  return [{ json: { reject: true, reason: 'signature_length_mismatch' } }];
}
const a = Buffer.from(sigHeader);
const b = Buffer.from(expected);
if (!crypto.timingSafeEqual(a, b)) {
  return [{ json: { reject: true, reason: 'signature_mismatch' } }];
}

// ---- Verified. Parse body for downstream nodes ------------------
let parsed;
try {
  parsed = JSON.parse(rawBody.toString('utf8'));
} catch (e) {
  return [{ json: { reject: true, reason: 'body_not_json' } }];
}

if (parsed.type === 'url_verification') {
  return [{
    json: {
      reject: false,
      eventType: 'url_verification',
      challenge: parsed.challenge,
    }
  }];
}

return [{
  json: {
    reject: false,
    eventType: parsed.type || 'unknown',
    body: parsed,
  }
}];
