#!/usr/bin/env python3
"""Surgically patch the live opsmemory-slack-ingest workflow in n8n's
postgres so the dispatch accepts multiple trigger emojis instead of
just `memo`. Avoids the "delete + import + re-map credentials" loop
the operator otherwise faces on every workflow tweak.

n8n architecture detail: at runtime the webhook engine reads from
`workflow_history` (the published version pointed to by
`workflow_entity.activeVersionId`), NOT from `workflow_entity.nodes`
directly. The latter is the editor's working draft. Both must be
updated for a live patch to take effect.

Run on Spark:
    python3 /tmp/patch_n8n_workflow_live.py
    docker restart n8n   (optional — n8n picks up changes on next webhook)

Idempotent: re-running just rewrites to the same target state.
"""
from __future__ import annotations

import json
import subprocess
import sys


WORKFLOW_NAME_LIKE = "opsmemory-slack-ingest%"

NORMALIZE_JS_NEW = """\
const TRIGGER_EMOJIS = [
  "memo","white_check_mark","ballot_box_with_check","pushpin","round_pushpin",
  "spiral_notepad","clipboard","pencil","pencil2","scroll",
  "page_facing_up","paperclip","inbox_tray","file_folder","bookmark",
  "raised_hands","ok_hand","muscle","eyes","point_up",
  "point_right","exclamation","fire","rotating_light","alarm_clock",
  "hourglass","stopwatch","calendar","date","briefcase"
];

const item = $input.first().json;
const body = item.body || {};
const ev = body.event || {};
const target = ev.item || {};

const reaction = ev.reaction || '';

return [{
  json: {
    ...item,
    slackEventType: ev.type || '',
    slackReaction: reaction,
    slackIsTriggerEmoji: TRIGGER_EMOJIS.includes(reaction),
    slackItemType: target.type || '',
    slackItemChannel: target.channel || '',
    slackItemTs: target.ts || '',
  }
}];
"""

# Codex 2026-05-09 review: split the 30 trigger emojis into "strong"
# (operator clearly tagging this as a task) vs "weak" (noting /
# acknowledging). Pass the intent through as
# source_metadata.extra.reaction_intent so the slack_message_extract
# v2 prompt can lower its skip threshold on strong, only nudge on weak.
BUILD_REACTION_JS_NEW = """\
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
  return [{ json: { skip: true, reason: 'slack_history_not_ok',
                    slack_error: slackResp?.error || 'unknown' } }];
}

const messages = Array.isArray(slackResp.messages) ? slackResp.messages : [];
if (messages.length === 0) {
  return [{ json: { skip: true, reason: 'message_not_found' } }];
}

const msg = messages[0];

if (msg.subtype === 'bot_message' || msg.bot_id) {
  return [{ json: { skip: true, reason: 'bot_message' } }];
}

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
  ts: msg.ts,
  text: text,
  user_id: msg.user || null,
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
    explicit_user_tagged: true,
    reactor_user_id: prep.slackUser,
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
"""


def fetch_target() -> tuple[str, str, list[dict]]:
    """Return (workflow_id, active_version_id, nodes_list)."""
    out = subprocess.check_output([
        "docker", "exec", "postgres", "psql", "-U", "openbrain", "-d", "n8n",
        "-t", "-A", "-F", "\x1f", "-c",
        "SELECT we.id, we.\"activeVersionId\", wh.nodes::text "
        "FROM workflow_entity we "
        "JOIN workflow_history wh ON wh.\"versionId\" = we.\"activeVersionId\" "
        f"WHERE we.name LIKE '{WORKFLOW_NAME_LIKE}' AND we.active = true LIMIT 1;",
    ], text=True).strip()
    if not out:
        sys.stderr.write("ERROR: no active opsmemory-slack-ingest workflow found\n")
        sys.exit(2)
    wid, vid, nodes_json = out.split("\x1f")
    return wid, vid, json.loads(nodes_json)


def fetch_credential_ids() -> dict[str, str]:
    """Return {credential_name: credential_id} for the Header Auth
    credentials we need."""
    out = subprocess.check_output([
        "docker", "exec", "postgres", "psql", "-U", "openbrain", "-d", "n8n",
        "-t", "-A", "-F", "\x1f", "-c",
        "SELECT id, name FROM credentials_entity WHERE type='httpHeaderAuth';",
    ], text=True).strip()
    out_map: dict[str, str] = {}
    for line in out.splitlines():
        if not line:
            continue
        cid, name = line.split("\x1f")
        out_map[name] = cid
    return out_map


def patch_nodes(nodes: list[dict], cred_ids: dict[str, str]) -> list[dict]:
    found_normalize = False
    found_if_memo = False
    found_slack_history = False

    ops_id = cred_ids.get("OpsMemory Service Key")
    slack_id = cred_ids.get("Slack bot token")
    if not ops_id or not slack_id:
        sys.stderr.write(
            f"ERROR: missing credential id(s). Have: {list(cred_ids)}\n"
        )
        sys.exit(5)

    for n in nodes:
        name = n.get("name")
        if name == "Normalize Slack event":
            n["parameters"]["jsCode"] = NORMALIZE_JS_NEW
            found_normalize = True
        elif name == "Build reaction body":
            n["parameters"]["jsCode"] = BUILD_REACTION_JS_NEW
        elif name == "IF reaction is memo?":
            cond_id = n["parameters"]["conditions"]["conditions"][0].get("id")
            n["parameters"]["conditions"]["conditions"] = [
                {
                    "id": cond_id or "patched-cond",
                    "leftValue": "={{ $json.slackIsTriggerEmoji }}",
                    "rightValue": True,
                    "operator": {
                        "type": "boolean",
                        "operation": "true",
                        "singleValue": True,
                    },
                }
            ]
            found_if_memo = True
        elif name == "Slack: fetch reacted message":
            # Force the credential reference to the actual Slack bot
            # token, not whatever the operator's last UI click left here.
            n["credentials"] = {
                "httpHeaderAuth": {"id": slack_id, "name": "Slack bot token"}
            }
            found_slack_history = True
        elif name in ("POST /v1/ingest/slack",
                      "POST /v1/ingest/slack (reaction)"):
            # And ensure both POST nodes use the OpsMemory key.
            n["credentials"] = {
                "httpHeaderAuth": {"id": ops_id, "name": "OpsMemory Service Key"}
            }

    if not found_normalize:
        sys.stderr.write("ERROR: 'Normalize Slack event' node not found\n")
        sys.exit(3)
    if not found_if_memo:
        sys.stderr.write("ERROR: 'IF reaction is memo?' node not found\n")
        sys.exit(3)
    if not found_slack_history:
        sys.stderr.write("ERROR: 'Slack: fetch reacted message' node not found\n")
        sys.exit(3)
    return nodes


def write_back(workflow_id: str, version_id: str, nodes: list[dict]) -> None:
    nodes_json = json.dumps(nodes)
    sql = (
        "BEGIN;\n"
        # Update the editor draft
        "UPDATE workflow_entity SET nodes = :'nodes'::jsonb, "
        "  \"updatedAt\" = now() WHERE id = :'wfid';\n"
        # Update the runtime active version (this is what n8n actually
        # executes for incoming webhook events)
        "UPDATE workflow_history SET nodes = :'nodes_history'::json, "
        "  \"updatedAt\" = now() WHERE \"versionId\" = :'vid';\n"
        "COMMIT;"
    )
    proc = subprocess.run([
        "docker", "exec", "-i", "postgres", "psql", "-U", "openbrain", "-d", "n8n",
        "-v", "ON_ERROR_STOP=1",
        "-v", f"wfid={workflow_id}",
        "-v", f"vid={version_id}",
        "-v", f"nodes={nodes_json}",
        "-v", f"nodes_history={nodes_json}",
    ], input=sql, text=True, capture_output=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        sys.exit(4)
    print(proc.stdout)


def main() -> int:
    wid, vid, nodes = fetch_target()
    cred_ids = fetch_credential_ids()
    print(f"workflow id:    {wid}")
    print(f"active version: {vid}")
    print(f"nodes count:    {len(nodes)}")
    print(f"credentials:    {list(cred_ids)}")
    nodes = patch_nodes(nodes, cred_ids)
    write_back(wid, vid, nodes)
    print("done. n8n picks up workflow_history changes without restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
