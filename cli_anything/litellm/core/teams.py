"""LiteLLM proxy team management (``/team/*``): create, update, delete, members, block.

Teams carry budgets, model access and rate limits that virtual keys inherit
(``keys generate --team``) and that users join (``users create --teams``), yet
until now they could only be created or changed in the Admin UI. This module
builds the request bodies for the ``/team/*`` mutations and resolves a CLI
argument (``team_id`` or unique ``team_alias``) against the live team list —
the same id-or-refusal pattern ``models`` and ``users`` already use.

Team ids vs aliases: the proxy generates a ``team_id`` when omitted, but keys
and users reference teams *by id*, so pinning one at create time
(``teams create --team-id …``) is usually worth it. The alias is the human
label shown in tables; deleting matches an exact ``team_id`` first, then a
*unique* alias — an ambiguous alias is refused, never guessed.

Deleting a team also deletes every virtual key minted for it
(``keys list --team`` names them first), and blocking one makes all of its
keys fail the same way (``blocked`` keys error with "Key blocked ...").
"""

from __future__ import annotations

from typing import Any

#: ``team_member_role`` values LiteLLM accepts inside ``members_with_roles``.
MEMBER_ROLES = ("admin", "user")

DEFAULT_MEMBER_ROLE = "user"


def normalize(t: dict[str, Any]) -> dict[str, Any]:
    """One row per team, as both the table and --json output show it."""
    return {
        "team_id": t.get("team_id"),
        "alias": t.get("team_alias"),
        "members": len(t.get("members_with_roles") or []),
        "spend": round(t.get("spend") or 0, 4),
        "max_budget": t.get("max_budget"),
        "rpm_limit": t.get("rpm_limit"),
        "tpm_limit": t.get("tpm_limit"),
        "models": t.get("models"),
        "blocked": t.get("blocked"),
    }


def new_team(
    alias: str | None,
    team_id: str | None = None,
    models: str | None = None,
    max_budget: float | None = None,
    rpm: int | None = None,
    tpm: int | None = None,
    budget_duration: str | None = None,
    members: tuple[str, ...] | list[str] | None = None,
    member_role: str | None = None,
) -> dict[str, Any]:
    """Build the POST /team/new body. Raises ValueError on a bad alias or member role.

    ``members`` are user references (names or emails) added as
    ``members_with_roles``; LiteLLM creates an account for an email that has
    none yet. ``budget_duration`` (e.g. ``30d``) resets ``max_budget`` periodically.
    """
    if not alias:
        raise ValueError("teams create needs --alias (team_alias shown in tables)")
    if member_role and member_role not in MEMBER_ROLES:
        raise ValueError(
            f"unknown --member-role {member_role!r}; LiteLLM team member roles: {', '.join(MEMBER_ROLES)}"
        )
    body: dict[str, Any] = {
        k: v
        for k, v in {
            "team_alias": alias,
            "team_id": team_id,
            "models": [m.strip() for m in models.split(",")] if models else None,
            "max_budget": max_budget,
            "rpm_limit": rpm,
            "tpm_limit": tpm,
            "budget_duration": budget_duration,
            "members_with_roles": [member_entry(m, member_role) for m in (members or ())] or None,
        }.items()
        if v is not None and v != []
    }
    return body


def update_team(
    team_id: str,
    alias: str | None = None,
    models: str | None = None,
    max_budget: float | None = None,
    rpm: int | None = None,
    tpm: int | None = None,
    budget_duration: str | None = None,
) -> dict[str, Any]:
    """Build the POST /team/update body. Raises ValueError on nothing-to-update.

    /team/update only changes fields it is given; omitted ones stay as-is.
    Set ``max_budget: null`` on a key, but on a team pass 0 to stop spend —
    LiteLLM's team endpoint drops a body field rather than unset it.
    """
    if not team_id:
        raise ValueError("identify the team with team_id (or a unique team_alias)")
    body = {
        k: v
        for k, v in {
            "team_alias": alias,
            "models": [m.strip() for m in models.split(",")] if models else None,
            "max_budget": max_budget,
            "rpm_limit": rpm,
            "tpm_limit": tpm,
            "budget_duration": budget_duration,
        }.items()
        if v is not None and v != []
    }
    if not body:
        raise ValueError("nothing to update")
    return {"team_id": team_id, **body}


def resolve_team_target(teams: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """Match ``ref`` against live teams, by exact team_id first, then unique alias.

    Returns ``{by, matches, team}`` or None — so the CLI refuses to mutate a
    team no live team matches, and refuses an alias two teams share rather
    than guessing (delete by team_id then).
    """
    if not ref:
        return None
    by_id = [t for t in teams if t.get("team_id") == ref]
    if by_id:
        return {"by": "team_id", "matches": [t.get("team_id") for t in by_id], "team": by_id[0]}
    by_alias = [t for t in teams if t.get("team_alias") == ref]
    if len(by_alias) > 1:
        return None  # ambiguous: refuse rather than guess which team
    if by_alias:
        return {"by": "team_alias", "matches": [by_alias[0].get("team_id")], "team": by_alias[0]}
    return None


def member_entry(ref: str, role: str | None = None) -> dict[str, Any]:
    """One ``members_with_roles`` / member entry: a name goes by user_id, an email by user_email.

    Passing ``user_email`` lets the proxy create the account when none exists;
    member_add on an unknown bare name would 404, so the CLI resolves those
    against `users list` first. Raises ValueError on an empty ref.
    """
    if not ref:
        raise ValueError("a member needs a user name or email")
    entry = {"user_email": ref} if "@" in ref else {"user_id": ref}
    if role:
        entry["team_member_role"] = role
    return entry
