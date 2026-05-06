#!/usr/bin/env python3
"""file_drop ingest canary (Chunk 9 step 5).

Smoke-tests the Drive -> n8n -> OpsMemory file-drop endpoint by
generating a CSV payload and an XLSX payload, POSTing both, and
asserting the responses match the runbook's contract.

Usage:
    OPSMEMORY_FILE_DROP_URL=https://opsmemory.kyleconway.ai \\
    OPSMEMORY_FILE_DROP_KEY=opsmem_live_... \\
    OPSMEMORY_FILE_DROP_BUSINESS=redhot \\
    python3 scripts/file_drop_canary.py

Env:
    OPSMEMORY_FILE_DROP_URL       (required) base URL of the API
    OPSMEMORY_FILE_DROP_KEY       (required) X-OpsMemory-Service-Key
                                  (slack:query NOT sufficient — needs
                                  ingest:write)
    OPSMEMORY_FILE_DROP_BUSINESS  (default: 'redhot')
    OPSMEMORY_FILE_DROP_FILE_ID   (default: auto-generated unique id;
                                  override to test idempotent retry)

Exit codes:
    0  all assertions passed
    1  configuration error
    2  CSV path failed
    3  XLSX path failed
    4  idempotent retry assertion failed

The script does NOT clean up the inserted ingest_events. Operator
runs once during deploy verification; the events sit at
status='received' until the worker fires (within 10s) and the
admin reviews them.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone


CSV_BODY_TEMPLATE = (
    "Task,Due,Owner,Category\n"
    "Smoke test from canary {tag},2026-08-01,Kyle,smoke\n"
    "Verify XLSX path,,Kyle,smoke\n"
)


def _post(url: str, key: str, body: dict) -> tuple[int, dict | None]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-OpsMemory-Service-Key": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body_json = json.loads(exc.read())
        except Exception:
            body_json = None
        return exc.code, body_json
    except Exception as exc:
        print(f"FAIL: network error posting to {url}: {exc!r}", file=sys.stderr)
        return -1, None


def _build_xlsx(text_rows: list[list[str]]) -> bytes:
    """Build a minimal XLSX in-memory via openpyxl.

    Importable only when openpyxl is installed (runtime dep added in
    chunk 9 step 3 requirements.txt). The canary is meant to be run
    after deploy from the same env where the API runs, so openpyxl
    will be present.
    """
    try:
        from openpyxl import Workbook
    except ImportError:
        print(
            "FAIL: openpyxl not installed in this environment. "
            "Install api/requirements.txt before running the canary.",
            file=sys.stderr,
        )
        sys.exit(1)
    wb = Workbook()
    # Default sheet name is 'Sheet'; rename to 'Tasks' so the XLSX
    # decode path's preferred-sheet selection picks it up.
    ws = wb.active
    ws.title = "Tasks"
    for row in text_rows:
        ws.append(row)
    # Add a second sheet that should be ignored.
    extra = wb.create_sheet(title="Reference")
    extra.append(["this", "sheet", "is", "ignored"])
    import io
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _expect(name: str, status: int, body: dict | None,
             expected_status: int) -> bool:
    if status != expected_status:
        print(f"FAIL [{name}]: HTTP {status} (expected {expected_status})",
              file=sys.stderr)
        if body is not None:
            print(f"  body: {json.dumps(body, indent=2)}", file=sys.stderr)
        return False
    print(f"PASS [{name}]: HTTP {status}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-idempotent", action="store_true",
                        help="Skip the second-post idempotent-retry check.")
    args = parser.parse_args()

    base_url = os.environ.get("OPSMEMORY_FILE_DROP_URL", "").rstrip("/")
    api_key = os.environ.get("OPSMEMORY_FILE_DROP_KEY", "")
    business_slug = os.environ.get("OPSMEMORY_FILE_DROP_BUSINESS", "redhot")
    if not base_url or not api_key:
        print(
            "ERROR: set OPSMEMORY_FILE_DROP_URL and OPSMEMORY_FILE_DROP_KEY",
            file=sys.stderr,
        )
        return 1

    endpoint = f"{base_url}/v1/ingest/file_drop"
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    tag = uuid.uuid4().hex[:8]
    file_id_csv = (os.environ.get("OPSMEMORY_FILE_DROP_FILE_ID")
                   or f"canary_csv_{tag}_aaaaaaaaaaaaaa")
    file_id_xlsx = (os.environ.get("OPSMEMORY_FILE_DROP_FILE_ID_XLSX")
                    or f"canary_xlsx_{tag}_bbbbbbbbbbbbbb")

    # ---- CSV path ----
    csv_body = {
        "file_id": file_id_csv,
        "modified_time": now_iso,
        "mime_type": "text/csv",
        "filename": f"canary_{tag}.csv",
        "business_slug": business_slug,
        "folder_ids": ["canary_folder"],
        "file_content": CSV_BODY_TEMPLATE.format(tag=tag),
    }
    print(f"\n--- CSV: POST {endpoint} (file_id={file_id_csv}) ---")
    status, body = _post(endpoint, api_key, csv_body)
    if not _expect("csv create", status, body, 201):
        return 2
    if body is None or body.get("source") != "file_drop":
        print("FAIL [csv create]: response missing source='file_drop'",
              file=sys.stderr)
        return 2
    if body.get("deduped") is not False:
        print(f"FAIL [csv create]: expected deduped=false, got {body.get('deduped')!r}",
              file=sys.stderr)
        return 2

    # ---- CSV idempotent retry ----
    if not args.skip_idempotent:
        print(f"\n--- CSV idempotent retry: POST {endpoint} (same file_id) ---")
        status2, body2 = _post(endpoint, api_key, csv_body)
        if not _expect("csv idempotent retry", status2, body2, 200):
            return 4
        if body2 is None or body2.get("deduped") is not True:
            print(f"FAIL [csv idempotent]: expected deduped=true, "
                  f"got {body2.get('deduped') if body2 else None!r}",
                  file=sys.stderr)
            return 4
        if body2.get("event_id") != body.get("event_id"):
            print(f"FAIL [csv idempotent]: event_id changed "
                  f"({body.get('event_id')} -> {body2.get('event_id')})",
                  file=sys.stderr)
            return 4

    # ---- XLSX path ----
    xlsx_bytes = _build_xlsx([
        ["Task", "Due", "Owner", "Category"],
        [f"XLSX canary {tag}", "2026-08-01", "Kyle", "smoke"],
        ["Verify Tasks-sheet selection", "", "Kyle", "smoke"],
    ])
    xlsx_b64 = base64.b64encode(xlsx_bytes).decode("ascii")
    xlsx_body = {
        "file_id": file_id_xlsx,
        "modified_time": now_iso,
        "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "filename": f"canary_{tag}.xlsx",
        "business_slug": business_slug,
        "folder_ids": ["canary_folder"],
        "xlsx_base64": xlsx_b64,
    }
    print(f"\n--- XLSX: POST {endpoint} (file_id={file_id_xlsx}, "
          f"xlsx_b64={len(xlsx_b64)} chars) ---")
    status, body = _post(endpoint, api_key, xlsx_body)
    if not _expect("xlsx create", status, body, 201):
        return 3
    if body is None or body.get("source") != "file_drop":
        print("FAIL [xlsx create]: response missing source='file_drop'",
              file=sys.stderr)
        return 3
    if body.get("deduped") is not False:
        print(f"FAIL [xlsx create]: expected deduped=false, "
              f"got {body.get('deduped')!r}", file=sys.stderr)
        return 3

    print("\nAll canary checks passed.")
    print(f"CSV  event_id: {body.get('event_id', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
