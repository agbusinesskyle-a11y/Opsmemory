// n8n Code node — build OpsMemory /v1/ingest/slack body from a
// reaction_added event whose target message has just been fetched.
//
// Drop this verbatim into the Code node AFTER the HTTP Request node
// that called Slack's conversations.history on the reaction branch.
//
// Input shape (the HTTP Request node forwards both its own response
// AND the prior node's data; in n8n 2.x with default settings the
// previous-node fields ride along on the same item):
//
//   $('Slack: fetch reacted message').first().json   -- conversations.history payload
//   $('Reaction prep').first().json                  -- prepared event metadata
//
// We read both via the `$('<NodeName>')` references rather than
// $input.first() so the input wiring is explicit and stable across
// n8n versions.
//
// Output shape: same as the mention-path Build OpsMemory body:
//   { skip: true, reason: '...' }                              -- drop silently
//   { skip: false, opsMemoryBody: {<SlackIngest body>}, ... }  -- forward to OpsMemory
//
// Filtering rules (top-level messages only at launch, per Codex
// 2026-05-08 review on threaded-reply behavior):
//   - conversations.history must return exactly one message (the one
//     we asked for via inclusive=true&limit=1).
//   - Reject if the message is a reply (thread_ts present and !==
//     the message ts itself).
//   - Reject bot messages and the bot's own messages so the bot
//     can't ingest itself.

// Codex 2026-05-09 review: split the 30 trigger emojis into "strong"
// (operator clearly tagging this as a task) vs "weak" (noting /
// acknowledging). Pass the intent through as source_metadata.extra
// .reaction_intent so the slack_message_extract prompt can lower
// its skip threshold on strong, and only nudge it on weak.
const STRONG_EMOJIS = new Set([
  "memo","white_check_mark","ballot_box_with_check","pushpin","round_pushpin",
  "spiral_notepad","clipboard","pencil","pencil2","scroll",
  "page_facing_up","paperclip","inbox_tray","file_folder","bookmark",
  "alarm_clock","hourglass","stopwatch","calendar","date","briefcase"
]);

const prep = $('Reaction prep').first().json;
const slackResp = $('Slack: fetch reacted message').first().json;

if (prep?.skip) {
  return [{ json: { skip: true, reason: prep.reason || 'upstream_skip' } }];
}

if (!slackResp || slackResp.ok !== true) {
  return [{
    json: {
      skip: true,
      reason: 'slack_history_not_ok',
      slack_error: slackResp?.error || 'unknown',
    }
  }];
}

const messages = Array.isArray(slackResp.messages) ? slackResp.messages : [];
if (messages.length === 0) {
  // The reacted-to message may have been deleted between Slack's
  // event delivery and our fetch. Drop silently — Slack won't retry
  // this delivery once we 200, and there's nothing to ingest.
  return [{ json: { skip: true, reason: 'message_not_found' } }];
}

const msg = messages[0];

// Drop bot messages.
if (msg.subtype === 'bot_message' || msg.bot_id) {
  return [{ json: { skip: true, reason: 'bot_message' } }];
}

// Top-level only at launch.
const isTopLevel = !msg.thread_ts || msg.thread_ts === msg.ts;
if (!isTopLevel) {
  return [{ json: { skip: true, reason: 'threaded_reply_not_supported_yet' } }];
}

const text = (msg.text || '').trim();
if (text.length < 1) {
  return [{ json: { skip: true, reason: 'empty_message_text' } }];
}

const opsMemoryBody = {
  team_id: prep.teamId,
  channel_id: prep.slackChannel,
  ts: msg.ts,                    // canonical event id is the original
                                  // message's ts, NOT the reaction's. Slack
                                  // retries on the SAME reaction event_id
                                  // but our idempotency in OpsMemory keys
                                  // off (team, channel, ts) so multiple
                                  // reactions on the same message dedupe
                                  // to one ingest_event.
  text: text,
  user_id: msg.user || null,      // ORIGINAL author, not the reactor
  thread_ts: msg.thread_ts || null,
  channel_name: null,
  user_name: null,
  team_domain: null,
  workspace_name: null,
  enterprise_id: null,
  extra: {
    slack_event_id: prep.eventId,
    slack_event_type: 'reaction_added',
    reaction: prep.reaction,
    reaction_intent: STRONG_EMOJIS.has(prep.reaction) ? 'strong' : 'weak',
    explicit_user_tagged: true,        // any reaction in trigger list = explicit signal
    reactor_user_id: prep.slackUser,   // who labeled this as a task
    original_text: msg.text || null,
  },
};

return [{
  json: {
    skip: false,
    opsMemoryBody,
    team_id: prep.teamId,
    channel_id: prep.slackChannel,
    ts: msg.ts,
  }
}];
