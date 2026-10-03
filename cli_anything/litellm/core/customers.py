"""LiteLLM proxy customer (end-user) management (``/customer/*``): list, inspect, create, update, block, delete.

A *customer* in LiteLLM is an end user of the app behind the proxy — a
``user_id`` in the proxy's end-user table against which the proxy tracks
``spend`` in real time, applies ``max_budget`` (inline or via a shared
``budget_id``), ``tpm``/``rpm`` limits, and a ``blocked`` switch. Virtual keys
accept ``user_id`` on request, so the app can charge or cap one app user
without minting a key for each — but until now those customers could only be
created or changed in the Admin UI, and the CLI could not list them.

The endpoints, as the v1.100 proxy has them: ``GET /customer/list`` (a bare
list on older proxies, a ``{"customers": [...]}`` page on newer ones),
``GET /customer/info?user_id=``, ``POST /customer/new``,
``POST /customer/update`` and ``POST /customer/delete`` — which takes
``{"user_ids": [...]}``, the only family member that speaks plural.

Customers are referenced by exact ``user_id`` primarily; the human ``alias``
resolves like a team alias: exact ``user_id`` first, then a *unique* alias —
an alias two customers share is refused, never guessed.

Deleting a customer stops the proxy attributing spend to them; it does not
delete any key.
"""

from __future__ import annotations

from typing import Any

#: ``/customer/delete`` (and ``/customer/block`` / ``/customer/unblock``) take
#: ``{"user_ids": [...]}``; the read and update endpoints are singular. Keeping
#: it in one place documents the inconsistency.
DELETE_KEY = "user_ids"


def as_rows(res: Any) -> list[dict[str, Any]]:
    """Whatever ``/customer/list`` returned, as a list of customer rows.

    Older proxies return a bare list; newer ones wrap a paginated ``customers``
    key (the shape ``{"data": [...]}`` exists on still-older builds). Tolerate
    all three so a shape change is a fixture, not a crash.
    """
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for key in ("customers", "data"):
            if isinstance(res.get(key), list):
                return res[key]
    return []


def normalize(c: dict[str, Any]) -> dict[str, Any]:
    """One row per customer, as both the table and --json output show it."""
    return {
        "user_id": c.get("user_id"),
        "alias": c.get("alias"),
        "spend": round(c.get("spend") or 0, 4),
        "max_budget": c.get("max_budget"),
        "budget_id": c.get("budget_id"),
        "tpm_limit": c.get("tpm_limit"),
        "rpm_limit": c.get("rpm_limit"),
        "blocked": c.get("blocked"),
    }


def new_customer(
    user_id: str | None = None,
    alias: str | None = None,
    max_budget: float | None = None,
    budget_id: str | None = None,
    rpm: int | None = None,
    tpm: int | None = None,
) -> dict[str, Any]:
    """Build the POST /customer/new body. Raises ValueError on nothing to go by.

    Every limit is optional, but a customer needs at least a ``user_id`` (what
    apps pass as ``user_id`` with each request) or an ``alias`` — the proxy
    generates a user id when both are missing, and an id nobody knows is an
    audit dead end, the way an implicitly minted key without an alias is.
    """
    if not user_id and not alias:
        raise ValueError("a customer needs --user-id (what apps pass as user_id) or --alias")
    return _fields(
        {
            "user_id": user_id,
            "alias": alias,
            "max_budget": max_budget,
            "budget_id": budget_id,
            "rpm_limit": rpm,
            "tpm_limit": tpm,
        }
    )


def update_customer(
    user_id: str | None = None,
    alias: str | None = None,
    max_budget: float | None = None,
    budget_id: str | None = None,
    rpm: int | None = None,
    tpm: int | None = None,
) -> dict[str, Any]:
    """Build the POST /customer/update body. Raises ValueError on nothing to update.

    /customer/update only changes fields it is given; omitted ones stay as-is.
    Attaching a ``budget_id`` makes the shared cap override ``max_budget``.
    """
    if not user_id:
        raise ValueError("identify the customer with USER_ID — run `customers list`")
    body = new_customer(user_id, alias, max_budget, budget_id, rpm, tpm)
    if len(body) < 2:
        raise ValueError("nothing to update: give --alias, --max-budget, --budget-id, --rpm or --tpm")
    return body


def resolve_customer_target(customers: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """Match ``ref`` against live customers, by exact ``user_id`` first, then alias.

    Returns ``{by, matches, customer}`` or None. An alias two customers share is
    ambiguous and refused — delete/block by ``user_id`` instead, like a shared
    email in ``users delete``.
    """
    if not ref:
        return None
    by_id = [c for c in customers if c.get("user_id") == ref]
    if by_id:
        return {"by": "user_id", "matches": [by_id[0].get("user_id")], "customer": by_id[0]}
    by_alias = [c for c in customers if c.get("alias") == ref]
    if len(by_alias) > 1:
        return None  # ambiguous: refuse rather than guess which customer
    if by_alias:
        return {"by": "alias", "matches": [by_alias[0].get("user_id")], "customer": by_alias[0]}
    return None


def ids_body(user_ids: str | list[str]) -> dict[str, Any]:
    """The plural ``{"user_ids": [...]}`` body block/unblock/delete send.

    Every member of the family that acts on a live customer speaks plural —
    after the CLI resolved one ref to its list of ids.
    """
    if isinstance(user_ids, str):
        user_ids = [user_ids]
    if not user_ids:
        raise ValueError("no customer to act on")
    return {DELETE_KEY: list(user_ids)}


def _fields(spec: dict[str, Any]) -> dict[str, Any]:
    """The body, minus fields nobody set (the proxy 400s on explicit nulls)."""
    return {k: v for k, v in spec.items() if v is not None and v != ""}
