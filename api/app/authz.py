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
