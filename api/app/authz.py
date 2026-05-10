"""Authorization helpers.

Cleanly-separated from auth.py (which decides WHO is making the request).
authz decides WHAT they can see/do.

Phase MT-2 (2026-05-10): users.role split into 'platform_admin' and
'owner'. 'admin' value still exists in the app_role enum because it
denotes per-business admin in business_memberships.role, which is
separate from platform-wide admin authority.

Ruleset:

  Platform admin (users.role='platform_admin'):
    - Kyle only (single-platform-owner deploy). May read all tasks,
      all businesses, all users.

  Owner (users.role='owner'):
    - May read tasks visible to any business they're a member of
      (via business_memberships.business_id ∈ task_businesses.business_id).
    - May read /v1/businesses scoped to their memberships.
    - May NOT read /v1/users (403).
    - business_memberships.role='admin' grants per-business admin
      authority but does NOT grant platform-wide visibility — that's
      the point of the MT-2 split. Joanna (owner of borderline +
      redhot, admin in those memberships) cannot see Conway Feed.

  Service (users.role='service'):
    - Default-deny on task visibility. Per-account scopes widen.
    - /v1/users is platform-admin-only regardless of scope.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from .auth import Principal


def require_admin(principal: Principal) -> None:
    """Raise 403 unless the principal is a platform admin.

    MT-2: 'platform_admin' is the new value. Existing call sites kept
    the require_admin name for diff minimisation. Per-business admin
    via business_memberships.role does NOT satisfy this gate.
    """
    if principal.principal_type == "user" and principal.role == "platform_admin":
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="platform admin required",
    )


# Standard scope strings issued by scripts/bootstrap_service_account.py.
# These are the only scopes wired into authz.require_scope below; new
# scopes ship with the route that consumes them.
SCOPE_INGEST_WRITE = "ingest:write"
SCOPE_TASKS_READ_ALL = "tasks:read:all"
SCOPE_BUSINESSES_READ = "businesses:read"
# Reconciliation pipeline read scope: a service account holding this
# scope sees all businesses for retrieval (otherwise Slack/email/Excel
# events ingested by a service principal would skip retrieval and
# produce only AMBIGUOUS/CREATE proposals). Per least-privilege, this
# is split from ingest:write so a write-only ingest key can't also
# read all businesses through the API.
SCOPE_PIPELINE_READ_ALL = "pipeline:read:all_businesses"

# Chunk 8: Slack /tasks slash-command bridge. n8n verifies Slack
# signing, normalizes the payload, then POSTs /v1/slack/tasks with a
# service key holding this scope. The endpoint maps the Slack
# user_id to a canonical OpsMemory user and applies that user's
# visibility semantics — the service key itself does NOT see all
# tasks, it just authenticates the n8n forwarder.
SCOPE_SLACK_QUERY = "slack:query"

# Chunk 12: read-only MCP. The mcp-server (api/mcp/server.py)
# acts as a service principal carrying ONLY this scope. It
# proxies stdio MCP tool calls (list_tasks, get_task,
# list_businesses) to the OpsMemory /v1 API. The narrow scope
# stops a leaked MCP key from doing anything beyond reads — no
# ingest, no slack, no pipeline access.
SCOPE_MCP_READ = "mcp:read"


def require_scope(principal: Principal, scope: str) -> None:
    """Allow if principal is admin user OR service-with-scope.

    Owners do not have scopes — they get role-based access via the routes
    that don't call this helper. require_scope is the gate for endpoints
    that admit machine callers (e.g. ingest endpoints called by n8n).
    """
    if principal.principal_type == "user" and principal.role == "platform_admin":
        return
    if scope in (principal.scopes or []):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"scope '{scope}' required",
    )


def visible_business_ids(principal: Principal) -> list[str] | None:
    """Return business ids the principal can see, or None for unrestricted.

    None means "no scoping — return everything". Admins get None.
    Owners get the list of their business_membership business ids.
    Services default-deny ([]) unless they hold a read scope:
      - SCOPE_TASKS_READ_ALL: legacy, unrestricted
      - SCOPE_MCP_READ: chunk 12, unrestricted (single-tenant
        deploy; revisit when multi-tenant lands)
      - SCOPE_PIPELINE_READ_ALL: reconciliation worker;
        the worker uses it for retrieval, but it ALSO unblocks
        the /v1 read path so a future audit query from the
        worker doesn't silently see nothing.
    """
    if principal.principal_type == "user" and principal.role == "platform_admin":
        return None
    if principal.principal_type == "user" and principal.role == "owner":
        return [b["id"] for b in principal.businesses]
    if principal.principal_type == "service":
        scopes = set(principal.scopes or [])
        if (SCOPE_TASKS_READ_ALL in scopes
                or SCOPE_MCP_READ in scopes
                or SCOPE_PIPELINE_READ_ALL in scopes):
            return None
        return []
    # Should be unreachable.
    return []
