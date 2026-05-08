#!/usr/bin/env python3
"""Build an importable n8n workflow JSON for the opsmemory-slack-ingest
flow described in docs/16-slack-n8n-build-runbook.md.

This is the one-shot alternative to the click-by-click runbook. The
output JSON imports cleanly into n8n 2.18.5 (and recent 2.x);
operator's only manual step after import is mapping the HTTP Request
node's Header Auth credential to the `OpsMemory Service Key (slack
ingest)` credential they already created.

Embeds the full bodies of:
  infra/n8n/slack-ingest-hmac-verify.js
  infra/n8n/slack-ingest-build-body.js

Topology:
  Webhook (POST /slack-ingest, raw body)
    -> HMAC Verify (Code)
       -> IF reject?
          true  -> Respond 401 (with reason)
          false -> IF url_verification?
             true  -> Respond 200 + {challenge}
             false -> Build OpsMemory body (Code)
                -> IF skip?
                   true  -> Respond 200 silent
                   false -> POST /v1/ingest/slack (HTTP Request)
                      -> Respond 200 final

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


def _id() -> str:
    return str(uuid.uuid4())


def build_workflow() -> dict:
    hmac_js = HMAC_JS_PATH.read_text(encoding="utf-8")
    build_js = BUILD_JS_PATH.read_text(encoding="utf-8")

    # Stable IDs so re-imports overwrite cleanly.
    nid_webhook = _id()
    nid_hmac = _id()
    nid_if_reject = _id()
    nid_resp_401 = _id()
    nid_if_urlv = _id()
    nid_resp_challenge = _id()
    nid_build = _id()
    nid_if_skip = _id()
    nid_resp_silent = _id()
    nid_http = _id()
    nid_resp_final = _id()

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
            "position": [240, 300],
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
            "position": [460, 300],
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
            "position": [680, 300],
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
            "position": [900, 200],
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
            "position": [900, 400],
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
            "position": [1120, 300],
        },
        {
            "parameters": {
                "language": "javaScript",
                "jsCode": build_js,
            },
            "id": nid_build,
            "name": "Build OpsMemory body",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [1120, 500],
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
            "position": [1340, 500],
        },
        {
            "parameters": {
                "respondWith": "noData",
                "options": {
                    "responseCode": 200,
                },
            },
            "id": nid_resp_silent,
            "name": "Respond 200 silent",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [1560, 400],
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
            "position": [1560, 600],
            "credentials": {
                "httpHeaderAuth": {
                    "name": "OpsMemory Service Key (slack ingest)"
                }
            },
        },
        {
            "parameters": {
                "respondWith": "noData",
                "options": {
                    "responseCode": 200,
                },
            },
            "id": nid_resp_final,
            "name": "Respond 200 final",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [1780, 600],
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
                [{"node": "Build OpsMemory body", "type": "main", "index": 0}],
            ]
        },
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
            "instanceId": "opsmemory-phase-c-import",
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
