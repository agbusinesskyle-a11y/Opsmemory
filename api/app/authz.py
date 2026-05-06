"""Authorization helpers.

Cleanly-separated from auth.py (which decides WHO is making the request).
authz decides WHAT they can see/do.

Chunk 2 ruleset (read-only):

  Admin (role='admin'):
    - May read all tasks across all businesses
    - May read /v1/users
    - May read /v1/businesses (full list)

  Owner (role='owner'):
    - May read tasks visible to any business they're a member of
      (via business_memberships.business_id ∈ task_businesses.business_id).
      Assignment is a stronger signal but visibility is by business.
    - May read /v1/businesses scoped to their memberships
    - May NOT read /v1/users (403)

  Service (role='service'):
    - Default-deny on task visibility — visible_business_ids returns []
      so /v1/tasks and /v1/tasks/{id} are empty/404 for all service
      principals. Per-account scopes (e.g. `tasks:read:all`,
      `ingest:write`, `businesses:read`) widen this in Chunk 3+ when
      the bootstrap CLI starts issuing keys with explicit scope lists.
    - /v1/users is admin-only regardless of scope.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from .auth import Principal


def require_admin(principal: Principal) -> None:
    """Raise 403 unless the principal is an admin user."""
    if principal.principal_type == "user" and principal.role == "admin":
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="admin role required",
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


def require_scope(principal: Principal, scope: str) -> None:
    """Allow if principal is admin user OR service-with-scope.

    Owners do not have scopes — they get role-based access via the routes
    that don't call this helper. require_scope is the gate for endpoints
    that admit machine callers (e.g. ingest endpoints called by n8n).
    """
    if principal.principal_type == "user" and principal.role == "admin":
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
    Services get [] (default-deny until per-scope rules land).
    """
    if principal.principal_type == "user" and principal.role == "admin":
        return None
    if principal.principal_type == "user" and principal.role == "owner":
        return [b["id"] for b in principal.businesses]
    # Service principals: default-deny task visibility for Chunk 2.
    # Specific scopes will widen this later.
    return []
