// n8n Code node — prepare a Slack conversations.history call.
//
// Drop this verbatim into the Code node BEFORE the HTTP Request node
// that calls Slack's API on the reaction_added branch. We use a Code
// node here (instead of binding query strings directly on the HTTP
// Request node) because reaction events have several layout variants
// — `item.type='message'` always, but `item.ts` is the canonical
// reacted-to message timestamp regardless of whether the original
// is top-level or threaded.
//
// Companion docs:
//   docs/15-slack-edge-adapter-contract.md (the contract)
//   docs/16-slack-n8n-build-runbook.md     (step-by-step n8n setup)
//
// Input (from the IF "reaction is memo?" upstream):
//   { reject: false, eventType: 'event_callback',
//     body: { event: { type: 'reaction_added', user, reaction,
//                       item: { type, channel, ts }, ... },
//             team_id, event_id, ... } }
//
// Output (single item) — fields the HTTP Request node will reference:
//   slackChannel : the channel id to query
//   slackLatest  : the message ts to look up
//   reaction     : the emoji name (passed through for build-body audit)
//   slackUser    : who reacted (for source_metadata.user_id; the
//                   ingester is the reactor, not the original author —
//                   the original author is recovered from the fetched
//                   message body)
//   teamId       : workspace id
//   eventId      : Slack event_id (idempotency, audit)
//   originalEventBody: the full event payload for downstream build.
//
// We don't strip thread_ts here — the build-body node decides whether
// threaded replies are accepted (they're skipped at launch per Codex
// 2026-05-08 review until cleanly tested).

const item = $input.first().json;
const body = item.body;
if (!body) {
  return [{ json: { skip: true, reason: 'no_body' } }];
}

const ev = body.event;
if (!ev || ev.type !== 'reaction_added') {
  return [{ json: { skip: true, reason: 'not_reaction_added' } }];
}

const target = ev.item;
if (!target || target.type !== 'message') {
  // Reactions can also be on file/file_comment items; we only handle
  // message reactions for the ingest path.
  return [{ json: { skip: true, reason: 'reaction_target_not_message' } }];
}

const channel = target.channel;
const ts = target.ts;
if (!channel || !ts) {
  return [{ json: { skip: true, reason: 'missing_channel_or_ts' } }];
}

return [{
  json: {
    skip: false,
    slackChannel: channel,
    slackLatest: ts,
    reaction: ev.reaction || null,
    slackUser: ev.user || null,
    teamId: body.team_id,
    eventId: body.event_id,
    originalEventBody: body,
  }
}];
