"""LiteLLM proxy user management (``/user/*``): list, inspect, create, update, delete.

Teams and keys both reference users, yet the CLI had no way to manage users, so
onboarding ("give svc-bot an account") fell back to the Admin UI. One page of
LiteLLM's user model this CLI leans on: a user is a database identity with a
role, optional team membership, limits and spend; virtual keys are minted *for*
it via ``keys generate --user``. Creating a user never mints a key
(``auto_create_key: false``) — a key with no alias is an audit dead end.

Roles are LiteLLM's, not ours: ``proxy_admin``, ``proxy_admin_viewer``,
``proxy_admin_writer``, ``internal_user``, ``internal_user_viewer``,
``internal_user_writer``, ``team``, ``customer``. ``internal_user`` is what an
ordinary account gets behind enterprise SSO; anything with ``proxy_admin`` can
read and mutate the whole proxy.
"""

from __future__ import annotations

from typing import Any

#: The roles a v1.100.x proxy accepts for ``user_role`` (validated client-side
#: so a typo reaches the operator before the proxy's opaque 400 does).
ROLES = (
    "proxy_admin",
    "proxy_admin_viewer",
    "proxy_admin_writer",
    "internal_user",
    "internal_user_viewer",
    "internal_user_writer",
    "team",
    "customer",
)

DEFAULT_ROLE = "internal_user"

# Deletion is not a fire-and-forget: removing a user also removes the virtual
# keys minted for them, so the delete flow says so and lists them first.
DELETE_TAKES_KEYS = True


def normalize(u: dict[str, Any]) -> dict[str, Any]:
    """One row per user, as both the table and --json output show it."""
    return {
        "user_id": u.get("user_id"),
        "email": u.get("user_email"),
        "role": u.get("user_role"),
        "teams": u.get("teams") or [],
        "max_budget": u.get("max_budget"),
        "rpm_limit": u.get("rpm_limit"),
        "tpm_limit": u.get("tpm_limit"),
        "blocked": u.get("blocked"),
        "spend": round(u.get("spend") or 0, 4),
        "models": u.get("models"),
    }


def new_user(
    user_id: str | None = None,
    email: str | None = None,
    role: str | None = None,
    teams: str | None = None,
    max_budget: float | None = None,
    rpm: int | None = None,
    tpm: int | None = None,
) -> dict[str, Any]:
    """Build the POST /user/new body. Raises ValueError on a bad user/role."""
    if not user_id and not email:
        raise ValueError("a user needs --user-id or --email")
    if role and role not in ROLES:
        raise ValueError(f"unknown --role {role!r}; LiteLLM roles: {', '.join(ROLES)}")
    body: dict[str, Any] = {
        k: v
        for k, v in {
            "user_id": user_id,
            "user_email": email,
            "user_role": role,
            "teams": [t.strip() for t in teams.split(",")] if teams else None,
            "max_budget": max_budget,
            "rpm_limit": rpm,
            "tpm_limit": tpm,
        }.items()
        if v is not None and v != []
    }
    # Minting a key here would create an unnamed key whose alias only an admin
    # can find in the DB; keys belong to `keys generate --alias … --user …`.
    body["auto_create_key"] = False
    return body


def update_user(
    user_id: str | None = None,
    email: str | None = None,
    role: str | None = None,
    teams: str | None = None,
    max_budget: float | None = None,
    rpm: int | None = None,
    tpm: int | None = None,
) -> dict[str, Any]:
    """Build the POST /user/update body. Raises ValueError on nothing-to-update or a bad role."""
    if not user_id and not email:
        raise ValueError("identify the user with --user-id or --email")
    body = new_user(user_id, email, role, teams, max_budget, rpm, tpm)
    body.pop("auto_create_key")  # /user/update only changes fields it is given
    return body


def resolve_user_target(users: list[dict], ref: str) -> dict[str, Any] | None:
    """Match ``ref`` against live users, by exact user_id first, then email.

    Returns ``{by, matches, user}`` or None — so the CLI can refuse to delete a
    user (user_id, or a *unique* email) that no live user matches, the way
    ``models delete`` refuses an unknown deployment.
    """
    if not ref:
        return None
    by_id = [u for u in users if u.get("user_id") == ref]
    if by_id:
        return {"by": "user_id", "matches": [u.get("user_id") for u in by_id], "user": by_id[0]}
    by_email = [u for u in users if u.get("user_email") == ref]
    if len(by_email) > 1:
        return None  # ambiguous: refuse rather than guess which account
    if by_email:
        return {"by": "user_email", "matches": [by_email[0].get("user_id")], "user": by_email[0]}
    return None
