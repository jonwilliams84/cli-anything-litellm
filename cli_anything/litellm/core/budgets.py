"""LiteLLM proxy budget management (``/budget/*``): list, inspect, create, update, delete.

A budget is a reusable spend-control object in the proxy's database: a
``max_budget`` (plus optional ``soft_budget`` warning), a ``budget_duration``
reset cycle (e.g. ``30d``), parallel/rate limits and per-model maxima
(``model_max_budget``). Keys, teams and users reference one by id —
``keys generate --budget-id`` — instead of carrying their own inline limits, so
the one place to re-price fifty keys next quarter is the budget, not fifty
key updates. Until now those budgets could only be created or changed in the
Admin UI, and the CLI could not even list them.

The endpoints, as the v1.100 proxy has them: ``GET /budget/list`` (the table
rows), ``GET /budget/info?budget_id=`` (the row plus the keys and teams on it),
``POST /budget/new``, ``POST /budget/update`` and ``POST /budget/delete``
(which takes ``{"id": ...}`` — the rest of the family says ``budget_id``,
delete is the exception). Deleting a budget leaves the attached keys, teams
and users working with their own inline limits; it does not delete them.

``budget_duration`` is LiteLLM's reset cycle, a number plus unit: seconds
(``1s``..) minutes (``1m``..), hours (``1h``..), days (``1d``..). Budgets are
referenced by exact ``budget_id`` — unlike teams and users there is no alias,
so no resolution step; the CLI refuses an argument nothing matches only in
that the proxy 404s, which surfaces through :class:`~..core.backend.ApiError`.
"""

from __future__ import annotations

import json
from typing import Any

#: ``/budget/delete`` says ``id``; every other endpoint in this family says
#: ``budget_id``. Keeping it in one place documents the inconsistency.
DELETE_KEY = "id"


def as_rows(res: Any) -> list[dict[str, Any]]:
    """Whatever ``/budget/list`` returned, as a list of budget rows.

    Older proxies return a bare list; newer ones wrap it. Tolerate ``list``,
    ``{"data": [...]}`` and ``{"budgets": [...]}`` so a shape change is a
    fixture, not a crash.
    """
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for key in ("data", "budgets"):
            if isinstance(res.get(key), list):
                return res[key]
    return []


def normalize(b: dict[str, Any]) -> dict[str, Any]:
    """One row per budget, as both the table and --json output show it."""
    return {
        "budget_id": b.get("budget_id"),
        "max_budget": b.get("max_budget"),
        "soft_budget": b.get("soft_budget"),
        "budget_duration": b.get("budget_duration"),
        "tpm_limit": b.get("tpm_limit"),
        "rpm_limit": b.get("rpm_limit"),
        "max_parallel_requests": b.get("max_parallel_requests"),
        "model_max_budget": b.get("model_max_budget"),
    }


def _model_max_budget(raw: str | dict[str, Any] | None) -> dict[str, float] | None:
    """Per-model caps: pass a dict or a JSON object string (``--help`` shows why).

    Raises ValueError on a string that is not a JSON object — building a body
    LiteLLM will 400 on helps nobody.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise ValueError(
            "--model-max-budget takes a JSON object mapping model → max, e.g. '{\"gpt-4o\": 0.01}'"
        ) from e
    if not isinstance(parsed, dict):
        raise ValueError("--model-max-budget takes a JSON object mapping model → max")
    return parsed


def new_budget(
    budget_id: str | None = None,
    max_budget: float | None = None,
    soft_budget: float | None = None,
    rpm: int | None = None,
    tpm: int | None = None,
    parallel: int | None = None,
    duration: str | None = None,
    model_max_budget: str | dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build the POST /budget/new body. Raises ValueError on nothing to cap.

    Every field is optional, but a budget with no limit and no reset cycle is
    a no-op — an admin would rather be told now than discover it in ``budgets
    list``. ``budget_id`` is optional too: the proxy generates one when
    omitted, but keys reference budgets *by id*, so pinning one (like
    ``teams create --team-id``) is usually worth it.
    """
    mm = _model_max_budget(model_max_budget)
    body: dict[str, Any] = {
        k: v
        for k, v in {
            "budget_id": budget_id,
            "max_budget": max_budget,
            "soft_budget": soft_budget,
            "rpm_limit": rpm,
            "tpm_limit": tpm,
            "max_parallel_requests": parallel,
            "budget_duration": duration,
            "model_max_budget": mm,
        }.items()
        if v is not None and v != {}
    }
    if not any(v is not None for k, v in body.items() if k != "budget_id"):
        raise ValueError(
            "a budget needs something to cap: --max-budget, --soft-budget, --rpm, "
            "--tpm, --parallel, --duration or --model-max-budget"
        )
    return body


def update_budget(
    budget_id: str,
    max_budget: float | None = None,
    soft_budget: float | None = None,
    rpm: int | None = None,
    tpm: int | None = None,
    parallel: int | None = None,
    duration: str | None = None,
    model_max_budget: str | dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build the POST /budget/update body. Raises ValueError on nothing to update.

    /budget/update only changes fields it is given; omitted ones stay as-is.
    The change applies to every key, team and user that references the budget.
    """
    if not budget_id:
        raise ValueError("identify the budget with BUDGET_ID — run `budgets list`")
    changes = new_budget(None, max_budget, soft_budget, rpm, tpm, parallel, duration, model_max_budget)
    return {"budget_id": budget_id, **changes}


def resolve_budget_target(budgets: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """Match ``ref`` against live budgets, by exact ``budget_id``.

    Returns ``{by, matches, budget}`` or None. Budgets have no alias, so this
    is an existence check rather than resolution — it lets the CLI refuse to
    delete a budget no live budget matches (pointing at ``budgets list``)
    instead of sending an id the proxy would have to 404.
    """
    if not ref:
        return None
    by_id = [b for b in budgets if b.get("budget_id") == ref]
    if by_id:
        return {"by": "budget_id", "matches": [by_id[0].get("budget_id")], "budget": by_id[0]}
    return None
