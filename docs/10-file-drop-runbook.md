# File-drop ingest runbook (Chunk 9)

> Operator contract for the Drive → n8n → OpsMemory file-drop pipeline.

## Overview

Operator drops a CSV or XLSX into a watched Google Drive folder. n8n
detects the change, downloads the file, identifies the business from
the folder, and POSTs the content to OpsMemory's
`POST /v1/ingest/file_drop` endpoint. OpsMemory parses (deterministic
CSV; LLM extract for free-form text), queues `review_items`, and an
admin approves them in the Review tab.

```
+---------------+        +-----------+        +------------+
| Drive folder  |  -->   |   n8n     |  -->   | OpsMemory  |  -->  Review tab
| RedHot/SOPs/  |        | (verify + |        | /v1/ingest |
| permits.xlsx  |        |  forward) |        | /file_drop |
+---------------+        +-----------+        +------------+
```

## One-time setup

### 1. Bootstrap a service account

OpsMemory authenticates the n8n forwarder via a dedicated service
account holding `ingest:write` scope. Run on the OpsMemory deploy box:

```bash
python3 scripts/bootstrap_service_account.py \
  --name n8n-file-drop \
  --description "n8n Drive file-drop bridge" \
  --scopes ingest:write
```

The script prints the raw key ONCE. Store it in n8n's credential
manager as `OPSMEMORY_FILE_DROP_KEY`. It will be sent as the
`X-OpsMemory-Service-Key` header on every forward.

> Codex chunk-9-close note: the service account does NOT need
> `pipeline:read:all_businesses`. The pipeline's deterministic
> resolver (`file_drop_resolve.py`) reads the business slug from the
> ingest's source_metadata, so retrieval is already scoped.

### 2. Folder → business mapping

This mapping lives in n8n workflow config. OpsMemory deliberately does
not store it server-side yet (per Codex chunk-9-step3 plan: routing
config, not a secret; add a DB-backed mapping when there's a UI for
it).

In the n8n workflow's "Resolve business" node, set up a JSON map:

```json
{
  "1AbCdEf...": "redhot",
  "2GhIjKl...": "borderline",
  "3MnOpQr...": "redhot"
}
```

Keys are Drive folder ids. Values are OpsMemory business slugs (must
match `businesses.slug` for an active business).

A folder id not in this map → n8n raises an alert and stops; do not
fall back to a default. Untagged folders create orphan ingest events.

### 3. Drive trigger

In n8n: Google Drive → "On changes to a specific folder". Add one
trigger per watched folder, OR (preferred) one trigger per parent
folder with a recursive watch and a folder-id check at the top of
the workflow.

## n8n workflow shape

```
[Drive trigger]
     │
     ▼
[Resolve folder id → business_slug]   (map lookup; no match → stop + alert)
     │
     ▼
[Download file binary]
     │
     ├──── if mime ∈ {text/csv, text/plain} ──► [Set file_content = binary as UTF-8]
     │
     ├──── if mime = application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
     │                                       ──► [Set xlsx_base64 = base64(binary)]
     │
     └──── else ──► [Alert + stop] (unsupported MIME)
     │
     ▼
[POST /v1/ingest/file_drop]   header: X-OpsMemory-Service-Key
     │
     ▼
[Log result] (deduped vs new; on error, alert)
```

## Request shape

`POST https://opsmemory.kyleconway.ai/v1/ingest/file_drop`

Headers:
```
Content-Type: application/json
X-OpsMemory-Service-Key: <opaque token from setup step 1>
```

Body — exactly one of `file_content` and `xlsx_base64` set:

```json
{
  "file_id":            "1AbCdEfGhIjKl...",
  "modified_time":      "2026-05-06T17:30:00Z",
  "mime_type":          "text/csv",
  "filename":           "permits.csv",
  "business_slug":      "redhot",
  "folder_ids":         ["1AbCdEf..."],
  "web_link":           "https://drive.google.com/file/d/.../view",
  "drive_owner_email":  "kyle@kyleconway.ai",

  "file_content":       "Task,Due,Owner\\nOrder containers,2026-08-01,Kyle\\n..."
}
```

XLSX variant — replace `file_content` with `xlsx_base64`:

```json
{
  "file_id":     "1AbCdEf...",
  "modified_time": "2026-05-06T17:30:00Z",
  "mime_type":   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "filename":    "permits.xlsx",
  "business_slug": "redhot",
  "folder_ids":  ["1AbCdEf..."],
  "xlsx_base64": "UEsDBBQA..."
}
```

`modified_time` must be ISO-8601 with timezone (Z or `±HH:MM`). The
endpoint canonicalizes it to UTC for the idempotency key.

## Response shapes

### Happy path — new event

`201 Created`

```json
{
  "event_id":    "8a7b6c5d-...",
  "status":      "received",
  "deduped":     false,
  "source":      "file_drop",
  "source_external_id": "drive:1AbCdEf...:2026-05-06T17:30:00+00:00"
}
```

### Idempotent retry — same (file_id, modified_time)

`200 OK`

```json
{
  "event_id":    "8a7b6c5d-...",
  "status":      "received",         // or pending_review / completed
  "deduped":     true,
  "dedup_key":   "source_external_id"
}
```

### Error codes

n8n should NOT retry these (deterministic operator misconfiguration):

| HTTP | code | meaning |
| ---- | ---- | ------- |
| 400  | `modified_time_required` / `modified_time_invalid` / `modified_time_naive` | malformed Drive timestamp |
| 415  | `binary_content_unsupported` | n8n posted raw bytes via `file_content`; should use `xlsx_base64` |
| 422  | `business_not_found` | business_slug isn't in `businesses` — n8n folder map is stale |
| 422  | `business_inactive` | business has been soft-deleted — fix the folder map |
| 422  | `xlsx_base64_invalid` / `xlsx_too_large` / `xlsx_bad_magic` / `xlsx_open_failed` / `xlsx_no_sheets` / `xlsx_no_recognized_sheet` / `xlsx_csv_too_large` | XLSX decode failed; alert operator |
| 422  | (Pydantic) | body shape wrong (e.g. both `file_content` and `xlsx_base64` set) |

n8n SHOULD retry with exponential backoff:

| HTTP | code | meaning |
| ---- | ---- | ------- |
| 503  | `xlsx_decode_unavailable` | API instance is missing openpyxl/defusedxml; resolves on next deploy |
| 5xx  | (any) | API down / DB unreachable |

## Sheet selection (XLSX)

When the workbook has multiple sheets, OpsMemory picks one:

1. **`Tasks` sheet wins** if present (case-insensitive match, must be
   visible).
2. Otherwise, the **first visible sheet whose first row contains a
   recognized task-summary header** (substring match against
   `summary` / `task` / `title` / `action` / `todo`).
3. No match → `422 xlsx_no_recognized_sheet`.

The selection is recorded in `source_metadata.xlsx_decode`:

```json
{
  "selected_sheet":  "Tasks",
  "ignored_sheets":  ["Reference", "Calc"],
  "row_count":       42,
  "col_count":       6,
  "decoded_bytes":   18432
}
```

## Caps

| Limit                              | Value           | Source                     |
| ---------------------------------- | --------------- | -------------------------- |
| `file_content` length              | 200,000 chars   | Pydantic                   |
| `xlsx_base64` length               | 8,000,000 chars | Pydantic                   |
| Decoded XLSX bytes                 | 5 MiB           | `xlsx_decode` (authoritative) |
| Converted CSV chars                | 200,000         | `xlsx_decode`              |
| Rows emitted per CSV               | 200             | `file_drop_parser`         |

A workbook that exceeds the row cap silently truncates after row 200
and logs `file_drop_parser_truncated`. Operator should split a wide
spreadsheet into multiple files.

## Pipeline behavior

1. n8n posts the file → `ingest_events.status='received'`.
2. The reconciliation worker (`scripts/run_pipeline.py`) claims the
   event (`source IN SOURCES.keys()` filter — `file_drop` is in
   `SOURCES`).
3. `extract.py` branches:
   - If `looks_like_csv(content)`: `parse_csv_candidates` produces
     candidates deterministically. **No LLM call.** parser_kind=`csv`
     stamped on each candidate.
   - Else: LLM extract using `file_drop_extract.v1` prompt.
4. `normalize.py` resolves owner aliases / dates / dedup keys.
5. `file_drop_resolve.py` forces `candidate.businesses` to the
   ingest's `business_slug` (LLM business hints are overridden;
   conflict logged but ingest mapping always wins).
6. `retrieve.py` searches existing tasks scoped to the business
   (`due_window=30d` + `recency_fallback=30d` per source config).
7. `choose.py` picks CREATE/UPDATE/COMPLETE/IGNORE/AMBIGUOUS.
8. `validate.py` schema + authz checks.
9. `review_items` row inserted, `ingest_events.status='pending_review'`.
10. Admin opens Review tab → approves → `tasks` row created.

## Auto-merge policy

`auto_merge_policy.enabled = false` for `(file_drop, *)` in chunk 9
seed (migration 0012). Every file-drop candidate goes to manual
review. Promote selectively after 30 days of observed approval rate.

## Observability

Server logs (single-line JSON):
- `ingest_file_drop_received` — every successful POST
- `xlsx_decoded` — XLSX decode result with sheet selection
- `xlsx_sheet_state_missing` — openpyxl returned a sheet without
  `sheet_state` (rare; fallback to "visible")
- `file_drop_parser_truncated` — row cap hit
- `file_drop_business_conflict` — LLM emitted a business hint that
  differs from the ingest mapping

n8n side: log every POST result; alert on:
- non-2xx responses except `409 sop_already_materialized` (not
  applicable here) and `200 deduped=true` (idempotent retry).
- timeouts > 10s.

## Smoke test (operator)

After deploying chunks 9 + dependencies (migration 0012, 0009-0011 if
fresh):

```bash
# CSV path
curl -sS -X POST https://opsmemory.kyleconway.ai/v1/ingest/file_drop \
  -H "Content-Type: application/json" \
  -H "X-OpsMemory-Service-Key: $OPSMEMORY_FILE_DROP_KEY" \
  -d '{
    "file_id":      "test_smoke_2026_05_06",
    "modified_time": "2026-05-06T17:00:00Z",
    "mime_type":   "text/csv",
    "filename":    "smoke.csv",
    "business_slug": "redhot",
    "file_content": "Task,Due,Owner\nSmoke test,2026-05-08,Kyle\n"
  }'
```

Expected: `201` with `event_id`. Re-run the same command → `200` with
`deduped: true`.

After the worker fires (10s default tick): `Review` tab shows one
pending review_item with summary "Smoke test" and resolved business
"redhot".

## Future work (deferred)

- DB-backed folder→business mapping with admin UI.
- Multi-sheet XLSX expansion (currently picks one sheet).
- Operator-defined per-folder CSV column mapping.
- `defusedxml`-based hardening upstream (currently auto-detected via
  openpyxl.DEFUSEDXML; readyz reports the status).
