// n8n Code node script — build OpsMemory /v1/ingest/slack body.
//
// Drop this verbatim into the Code node AFTER the IF router that
// has separated event_callback events from url_verification.
//
// Input (from HMAC-verify Code node):
//   { reject: false, eventType: 'event_callback', body: {<full slack event>} }
//
// Output (single item, schema below):
//   skip=true      -> downstream IF routes to "Respond 200 silent" (drop)
//   skip=false     -> downstream HTTP Request node POSTs opsMemoryBody
//
// Filtering rules for launch (per Codex Phase C trim):
//   - Only app_mention events go through. Every other subtype
//     (message.channels, message.im, reaction_added, etc.)
//     is dropped silently with skip=true. Reactions are explicitly
//     deferred until threaded-message handling is tested.
//   - Empty messages after stripping the bot mention are dropped.
//   - Bot messages (subtype=bot_message OR ev.bot_id) are dropped
//     to avoid the bot ingesting itself.
//
// Mention stripping:
//   Slack delivers mention text as "<@U01OPSMEM> rest of message".
//   We strip the LEADING bot mention so the LLM extract step doesn't
//   waste tokens on the routing token. Original is preserved in
//   extra.original_text for audit reproduction.

const item = $input.first().json;

// Defensive: if upstream node passed reject:true through somehow,
// drop. (The IF router in front of this node should have handled
// it, but belt-and-braces.)
if (item.reject) {
  return [{ json: { skip: true, reason: 'upstream_reject' } }];
}

const body = item.body;
if (!body) {
  return [{ json: { skip: true, reason: 'no_body' } }];
}

if (body.type !== 'event_callback') {
  return [{ json: { skip: true, reason: 'not_event_callback' } }];
}

const ev = body.event ?? {};
if (ev.type !== 'app_mention') {
  return [{ json: { skip: true, reason: `event_type_${ev.type ?? 'missing'}` } }];
}

// Drop bot messages (bot ingesting itself, or another bot in channel).
if (ev.subtype === 'bot_message' || ev.bot_id) {
  return [{ json: { skip: true, reason: 'bot_message' } }];
}

// Strip leading <@U...> mentions. Slack only routes app_mention to
// us when the bot is mentioned, but the user might have multiple
// mentions in one message ("<@OPSMEMORY> please assign <@JOANNA>...").
// We only strip OUR bot's mention if we know the bot user id; for
// safety strip ALL leading mentions until non-mention content.
const originalText = ev.text ?? '';
const mentionStripped = originalText
  .replace(/^(\s*<@[UW][A-Z0-9]+>\s*)+/, '')
  .trim();

if (mentionStripped.length < 1) {
  return [{ json: { skip: true, reason: 'empty_after_mention_strip' } }];
}

// Build the OpsMemory /v1/ingest/slack body.
// Schema source: api/app/v1_ingest.py SlackIngest pydantic model.
const opsMemoryBody = {
  team_id: body.team_id,
  channel_id: ev.channel,
  ts: ev.ts,
  text: mentionStripped,
  user_id: ev.user ?? null,
  thread_ts: ev.thread_ts ?? null,
  channel_name: null,        // populate via channels:read API if desired
  user_name: null,           // populate via users:read API if desired
  team_domain: null,
  workspace_name: null,
  enterprise_id: body.authorizations?.[0]?.enterprise_id ?? null,
  extra: {
    slack_event_id: body.event_id,
    slack_event_type: 'app_mention',
    original_text: originalText,
    api_app_id: body.api_app_id,
  },
};

return [{
  json: {
    skip: false,
    opsMemoryBody,
    // Convenience fields for n8n IF nodes / logs:
    team_id: body.team_id,
    channel_id: ev.channel,
    ts: ev.ts,
  }
}];
