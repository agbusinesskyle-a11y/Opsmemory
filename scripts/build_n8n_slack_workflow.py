#!/usr/bin/env python3
"""Build an importable n8n workflow JSON for the opsmemory-slack-ingest
flow described in docs/16-slack-n8n-build-runbook.md.

V2 (2026-05-08): adds the reaction_added ingest path. Posting in
Slack with `@OpsMemory ...` continues to work as before; *also*
react to any past message in a mapped channel with `:memo:` to
file that message as a task.

Embeds the full bodies of:
  infra/n8n/slack-ingest-hmac-verify.js          (HMAC verifier)
  infra/n8n/slack-ingest-build-body.js           (mention path body builder)
  infra/n8n/slack-ingest-fetch-message.js        (reaction-prep before Slack API)
  infra/n8n/slack-ingest-build-reaction-body.js  (reaction path body builder)

Topology:
  Webhook (POST /slack-ingest, raw body)
    -> HMAC Verify (Code)
       -> IF reject?
          true  -> Respond 401 (with reason)
          false -> IF url_verification?
             true  -> Respond 200 + {challenge}
             false -> IF reaction_added?
                true (reaction event):
                   -> IF reaction is memo?
                      true:
                         -> Reaction prep (Code)
                         -> Slack: fetch reacted message (HTTP)
                         -> Build reaction body (Code)
                         -> IF reaction skip?
                            true  -> Respond 200 reaction silent
                            false -> POST /v1/ingest/slack (reaction)
                                  -> Respond 200 reaction final
                      false -> Respond 200 wrong emoji
                false (mention or other event_callback):
                   -> Build OpsMemory body (mention) (Code)
                   -> IF skip?
                      true  -> Respond 200 mention silent
                      false -> POST /v1/ingest/slack (mention)
                            -> Respond 200 mention final

Trigger emoji: `:memo:` (built-in, available in every workspace).
To change: edit `MEMO_EMOJI` below, regenerate, reimport.

Usage:
    python3 scripts/build_n8n_slack_workflow.py \\
      > infra/n8n/opsmemory-slack-ingest.workflow.json
"""
from __future__ import annotations

import json
import pathlib
import sys
import uuid


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HMAC_JS_PATH = REPO_ROOT / "infra" / "n8n" / "slack-ingest-hmac-verify.js"
BUILD_JS_PATH = REPO_ROOT / "infra" / "n8n" / "slack-ingest-build-body.js"
PREP_JS_PATH = REPO_ROOT / "infra" / "n8n" / "slack-ingest-fetch-message.js"
REACTION_JS_PATH = REPO_ROOT / "infra" / "n8n" / "slack-ingest-build-reaction-body.js"

MEMO_EMOJI = "memo"  # without colons; matches reaction.event.reaction


# Codex post-launch v2 review (2026-05-08): flatten body.event.* into
# top-level fields so the IF dispatch nodes can route on simple
# string equality instead of optional-chained `body?.event?.type`
# expressions, which were silently routing the reaction event down
# the wrong branch in n8n 2.18.5. Also makes the workflow human-
# readable in the n8n editor — the IF parameters show
# `slackEventType === 'reaction_added'` instead of nested chain.
NORMALIZE_JS = """\
// Normalize Slack event — flatten body.event.* into top-level fields
// so downstream IF dispatch nodes can route on $json.slackEventType
// and $json.slackReaction directly. Codex 2026-05-08 review found
// optional-chained expressions in IF nodes were silently routing
// reaction events down the wrong branch. The fix is to do the
// chain-walk once here, in JS, and surface flat fields.

const item = $input.first().json;
const body = item.body || {};
const ev = body.event || {};
const target = ev.item || {};

return [{
  json: {
    // Pass through everything from HMAC Verify so downstream code
    // nodes can still see body.* if they need it.
    ...item,
    // New flat fields the IF dispatch nodes route on:
    slackEventType: ev.type || '',
    slackReaction: ev.reaction || '',
    slackItemType: target.type || '',
    slackItemChannel: target.channel || '',
    slackItemTs: target.ts || '',
  }
}];
"""


def _id() -> str:
    return str(uuid.uuid4())


def build_workflow() -> dict:
    hmac_js = HMAC_JS_PATH.read_text(encoding="utf-8")
    build_js = BUILD_JS_PATH.read_text(encoding="utf-8")
    prep_js = PREP_JS_PATH.read_text(encoding="utf-8")
    reaction_js = REACTION_JS_PATH.read_text(encoding="utf-8")

    # Stable IDs so re-imports overwrite cleanly.
    nid_webhook = _id()
    nid_hmac = _id()
    nid_if_reject = _id()
    nid_resp_401 = _id()
    nid_if_urlv = _id()
    nid_resp_challenge = _id()
    nid_normalize = _id()
    nid_if_reaction = _id()
    nid_if_prep_skip = _id()  # NEW: IF skip? after Reaction prep
    nid_resp_prep_skip = _id()  # NEW: silent drop on prep skip

    # Mention path
    nid_build = _id()
    nid_if_skip = _id()
    nid_resp_silent = _id()
    nid_http = _id()
    nid_resp_final = _id()

    # Reaction path
    nid_if_memo = _id()
    nid_resp_wrong_emoji = _id()
    nid_reaction_prep = _id()
    nid_slack_history = _id()
    nid_build_reaction = _id()
    nid_if_skip_reaction = _id()
    nid_resp_silent_reaction = _id()
    nid_http_reaction = _id()
    nid_resp_final_reaction = _id()

    # ---- Layout columns (px). Top half is mention; bottom is reaction. ----
    Y_TOP = 200    # 401 reject branch
    Y_MID = 300    # main flow
    Y_MENT = 460   # mention sub-flow
    Y_REACT = 700  # reaction sub-flow
    XCOL = lambda n: 240 + n * 220  # noqa: E731

    nodes = [
        {
            "parameters": {
                "httpMethod": "POST",
                "path": "slack-ingest",
                "responseMode": "responseNode",
                "options": {"rawBody": True},
            },
            "id": nid_webhook,
            "name": "Webhook",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [XCOL(0), Y_MID],
            "webhookId": _id(),
        },
        {
            "parameters": {
                "language": "javaScript",
                "jsCode": hmac_js,
            },
            "id": nid_hmac,
            "name": "HMAC Verify",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [XCOL(1), Y_MID],
        },
        {
            "parameters": {
                "conditions": {
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                    },
                    "conditions": [
                        {
                            "id": _id(),
                            "leftValue": "={{ $json.reject }}",
                            "rightValue": True,
                            "operator": {
                                "type": "boolean",
                                "operation": "true",
                                "singleValue": True,
                            },
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
            "id": nid_if_reject,
            "name": "IF reject?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2,
            "position": [XCOL(2), Y_MID],
        },
        {
            "parameters": {
                "respondWith": "text",
                "responseBody": "={{ $json.reason }}",
                "options": {
                    "responseCode": 401,
                },
            },
            "id": nid_resp_401,
            "name": "Respond 401 rejected",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [XCOL(3), Y_TOP],
        },
        {
            "parameters": {
                "conditions": {
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                    },
                    "conditions": [
                        {
                            "id": _id(),
                            "leftValue": "={{ $json.eventType }}",
                            "rightValue": "url_verification",
                            "operator": {
                                "type": "string",
                                "operation": "equals",
                            },
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
            "id": nid_if_urlv,
            "name": "IF url_verification?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2,
            "position": [XCOL(3), Y_MID + 100],
        },
        {
            "parameters": {
                "respondWith": "json",
                "responseBody": "={ \"challenge\": \"{{ $json.challenge }}\" }",
                "options": {
                    "responseCode": 200,
                },
            },
            "id": nid_resp_challenge,
            "name": "Respond 200 challenge",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [XCOL(4), Y_MID],
        },
        # ----- Normalize Slack event (flatten body.event.* to top-level) -----
        # Codex 2026-05-08 review: optional-chained expressions in n8n
        # IF nodes were silently routing reaction events the wrong way.
        # This Code node walks body.event.* once and surfaces the
        # critical fields as plain top-level strings so downstream
        # IF nodes can compare with simple equality.
        {
            "parameters": {
                "language": "javaScript",
                "jsCode": NORMALIZE_JS,
            },
            "id": nid_normalize,
            "name": "Normalize Slack event",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [XCOL(4), Y_MID + 100],
        },
        # ----- Dispatch by event type (reaction vs mention/other) -----
        {
            "parameters": {
                "conditions": {
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                    },
                    "conditions": [
                        {
                            "id": _id(),
                            "leftValue": "={{ $json.slackEventType }}",
                            "rightValue": "reaction_added",
                            "operator": {
                                "type": "string",
                                "operation": "equals",
                            },
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
            "id": nid_if_reaction,
            "name": "IF reaction_added?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2,
            "position": [XCOL(4), Y_MID + 200],
        },
        # ===== Mention path (false branch of IF reaction_added?) =====
        {
            "parameters": {
                "language": "javaScript",
                "jsCode": build_js,
            },
            "id": nid_build,
            "name": "Build OpsMemory body",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [XCOL(5), Y_MENT],
        },
        {
            "parameters": {
                "conditions": {
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                    },
                    "conditions": [
                        {
                            "id": _id(),
                            "leftValue": "={{ $json.skip }}",
                            "rightValue": True,
                            "operator": {
                                "type": "boolean",
                                "operation": "true",
                                "singleValue": True,
                            },
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
            "id": nid_if_skip,
            "name": "IF skip?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2,
            "position": [XCOL(6), Y_MENT],
        },
        {
            "parameters": {
                "respondWith": "noData",
                "options": {"responseCode": 200},
            },
            "id": nid_resp_silent,
            "name": "Respond 200 silent",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [XCOL(7), Y_MENT - 60],
        },
        {
            "parameters": {
                "method": "POST",
                "url": "http://opsmemory-api:8000/v1/ingest/slack",
                "authentication": "predefinedCredentialType",
                "nodeCredentialType": "httpHeaderAuth",
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify($json.opsMemoryBody) }}",
                "options": {
                    "timeout": 5000,
                    "response": {"response": {"neverError": True}},
                },
            },
            "id": nid_http,
            "name": "POST /v1/ingest/slack",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [XCOL(7), Y_MENT + 60],
            "credentials": {
                "httpHeaderAuth": {
                    "name": "OpsMemory Service Key (slack ingest)"
                }
            },
        },
        {
            "parameters": {
                "respondWith": "noData",
                "options": {"responseCode": 200},
            },
            "id": nid_resp_final,
            "name": "Respond 200 final",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [XCOL(8), Y_MENT + 60],
        },
        # ===== Reaction path (true branch of IF reaction_added?) =====
        {
            "parameters": {
                "conditions": {
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                    },
                    "conditions": [
                        {
                            "id": _id(),
                            "leftValue": "={{ $json.slackReaction }}",
                            "rightValue": MEMO_EMOJI,
                            "operator": {
                                "type": "string",
                                "operation": "equals",
                            },
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
            "id": nid_if_memo,
            "name": "IF reaction is memo?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2,
            "position": [XCOL(5), Y_REACT],
        },
        {
            "parameters": {
                "respondWith": "noData",
                "options": {"responseCode": 200},
            },
            "id": nid_resp_wrong_emoji,
            "name": "Respond 200 wrong emoji",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [XCOL(6), Y_REACT + 120],
        },
        {
            "parameters": {
                "language": "javaScript",
                "jsCode": prep_js,
            },
            "id": nid_reaction_prep,
            "name": "Reaction prep",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [XCOL(6), Y_REACT - 60],
        },
        # IF the prep returned skip:true (reaction not on a message,
        # missing channel/ts, etc), bail to a 200 silent before we'd
        # otherwise call Slack API with undefined params. Codex
        # 2026-05-08 review caught this — the original v2 always
        # called conversations.history regardless of prep output.
        {
            "parameters": {
                "conditions": {
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                    },
                    "conditions": [
                        {
                            "id": _id(),
                            "leftValue": "={{ $json.skip }}",
                            "rightValue": True,
                            "operator": {
                                "type": "boolean",
                                "operation": "true",
                                "singleValue": True,
                            },
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
            "id": nid_if_prep_skip,
            "name": "IF prep skip?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2,
            "position": [XCOL(6) + 100, Y_REACT - 60],
        },
        {
            "parameters": {
                "respondWith": "noData",
                "options": {"responseCode": 200},
            },
            "id": nid_resp_prep_skip,
            "name": "Respond 200 prep skip",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [XCOL(6) + 100, Y_REACT - 200],
        },
        {
            "parameters": {
                "method": "GET",
                "url": "https://slack.com/api/conversations.history",
                "authentication": "predefinedCredentialType",
                "nodeCredentialType": "httpHeaderAuth",
                "sendQuery": True,
                "queryParameters": {
                    "parameters": [
                        {"name": "channel",
                         "value": "={{ $json.slackChannel }}"},
                        # Bracket the target ts on both sides so Slack
                        # returns exactly the message we want, not the
                        # nearest visible neighbor (Codex review).
                        {"name": "oldest",
                         "value": "={{ $json.slackLatest }}"},
                        {"name": "latest",
                         "value": "={{ $json.slackLatest }}"},
                        {"name": "inclusive", "value": "true"},
                        {"name": "limit", "value": "1"},
                    ]
                },
                "options": {
                    "timeout": 4000,
                    "response": {"response": {"neverError": True}},
                },
            },
            "id": nid_slack_history,
            "name": "Slack: fetch reacted message",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [XCOL(7), Y_REACT - 60],
            "credentials": {
                "httpHeaderAuth": {
                    "name": "Slack Bot Token"
                }
            },
        },
        {
            "parameters": {
                "language": "javaScript",
                "jsCode": reaction_js,
            },
            "id": nid_build_reaction,
            "name": "Build reaction body",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [XCOL(8), Y_REACT - 60],
        },
        {
            "parameters": {
                "conditions": {
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                    },
                    "conditions": [
                        {
                            "id": _id(),
                            "leftValue": "={{ $json.skip }}",
                            "rightValue": True,
                            "operator": {
                                "type": "boolean",
                                "operation": "true",
                                "singleValue": True,
                            },
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
            "id": nid_if_skip_reaction,
            "name": "IF reaction skip?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2,
            "position": [XCOL(9), Y_REACT - 60],
        },
        {
            "parameters": {
                "respondWith": "noData",
                "options": {"responseCode": 200},
            },
            "id": nid_resp_silent_reaction,
            "name": "Respond 200 reaction silent",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [XCOL(10), Y_REACT - 120],
        },
        {
            "parameters": {
                "method": "POST",
                "url": "http://opsmemory-api:8000/v1/ingest/slack",
                "authentication": "predefinedCredentialType",
                "nodeCredentialType": "httpHeaderAuth",
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify($json.opsMemoryBody) }}",
                "options": {
                    "timeout": 5000,
                    "response": {"response": {"neverError": True}},
                },
            },
            "id": nid_http_reaction,
            "name": "POST /v1/ingest/slack (reaction)",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [XCOL(10), Y_REACT],
            "credentials": {
                "httpHeaderAuth": {
                    "name": "OpsMemory Service Key (slack ingest)"
                }
            },
        },
        {
            "parameters": {
                "respondWith": "noData",
                "options": {"responseCode": 200},
            },
            "id": nid_resp_final_reaction,
            "name": "Respond 200 reaction final",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [XCOL(11), Y_REACT],
        },
    ]

    connections = {
        "Webhook": {
            "main": [[{"node": "HMAC Verify", "type": "main", "index": 0}]]
        },
        "HMAC Verify": {
            "main": [[{"node": "IF reject?", "type": "main", "index": 0}]]
        },
        "IF reject?": {
            "main": [
                # true branch -> 401
                [{"node": "Respond 401 rejected", "type": "main", "index": 0}],
                # false branch -> IF url_verification?
                [{"node": "IF url_verification?", "type": "main", "index": 0}],
            ]
        },
        "IF url_verification?": {
            "main": [
                [{"node": "Respond 200 challenge", "type": "main", "index": 0}],
                [{"node": "Normalize Slack event", "type": "main", "index": 0}],
            ]
        },
        "Normalize Slack event": {
            "main": [[{"node": "IF reaction_added?", "type": "main", "index": 0}]]
        },
        "IF reaction_added?": {
            "main": [
                # true branch -> reaction sub-flow
                [{"node": "IF reaction is memo?", "type": "main", "index": 0}],
                # false branch -> mention sub-flow
                [{"node": "Build OpsMemory body", "type": "main", "index": 0}],
            ]
        },
        # ----- Mention sub-flow -----
        "Build OpsMemory body": {
            "main": [[{"node": "IF skip?", "type": "main", "index": 0}]]
        },
        "IF skip?": {
            "main": [
                [{"node": "Respond 200 silent", "type": "main", "index": 0}],
                [{"node": "POST /v1/ingest/slack", "type": "main", "index": 0}],
            ]
        },
        "POST /v1/ingest/slack": {
            "main": [[{"node": "Respond 200 final", "type": "main", "index": 0}]]
        },
        # ----- Reaction sub-flow -----
        "IF reaction is memo?": {
            "main": [
                [{"node": "Reaction prep", "type": "main", "index": 0}],
                [{"node": "Respond 200 wrong emoji", "type": "main", "index": 0}],
            ]
        },
        "Reaction prep": {
            "main": [[{"node": "IF prep skip?", "type": "main", "index": 0}]]
        },
        "IF prep skip?": {
            "main": [
                # true branch (prep returned skip:true) -> 200 silent
                [{"node": "Respond 200 prep skip", "type": "main", "index": 0}],
                # false branch -> proceed to Slack history
                [{"node": "Slack: fetch reacted message", "type": "main", "index": 0}],
            ]
        },
        "Slack: fetch reacted message": {
            "main": [[{"node": "Build reaction body", "type": "main", "index": 0}]]
        },
        "Build reaction body": {
            "main": [[{"node": "IF reaction skip?", "type": "main", "index": 0}]]
        },
        "IF reaction skip?": {
            "main": [
                [{"node": "Respond 200 reaction silent", "type": "main", "index": 0}],
                [{"node": "POST /v1/ingest/slack (reaction)", "type": "main", "index": 0}],
            ]
        },
        "POST /v1/ingest/slack (reaction)": {
            "main": [[{"node": "Respond 200 reaction final", "type": "main", "index": 0}]]
        },
    }

    return {
        "name": "opsmemory-slack-ingest",
        "nodes": nodes,
        "connections": connections,
        "active": False,
        "settings": {
            "executionOrder": "v1",
        },
        "pinData": {},
        "versionId": _id(),
        "meta": {
            "instanceId": "opsmemory-phase-c-import-v2",
        },
        "id": "opsmemory-slack-ingest",
        "tags": [],
    }


def main() -> int:
    wf = build_workflow()
    json.dump(wf, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
