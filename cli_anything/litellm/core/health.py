"""Recorded deployment health: what the proxy's own health checks last saw.

``health`` (``GET /health``) sends a real completion to every deployment. The
proxy also records health checks in its database, ``LiteLLM_HealthCheckTable``,
and serves them back without calling any backend (v1.106.3,
``litellm/proxy/health_endpoints/_health_endpoints.py``):

- ``GET /health/latest`` (``latest_health_checks_endpoint``):
  ``{"latest_health_checks": {<model_id or model_name>: row}, "total_models"}``,
  one row per (model_id, model_name) (``db/health_check_latest.py``).
- ``GET /health/history`` (``health_check_history_endpoint``): ``model``,
  ``status_filter``, ``limit`` (1..1000), ``offset``;
  ``{"health_checks": [row, ...], "total_records", "limit", "offset"}``,
  newest first.

A row is ``convert_health_check_to_dict``: health_check_id, model_name,
model_id, status (``healthy`` | ``unhealthy``), healthy_count,
unhealthy_count, error_message (the first unhealthy endpoint's error, cut to
500 characters), response_time_ms, details, checked_by, checked_at,
created_at.

What gets recorded: every manual ``/health`` call, and background health
checks (``background_health_checks: true``) ONLY WHEN A DEPLOYMENT'S STATUS
CHANGES (``save_background_health_checks_to_db``). So the history is a log of
transitions, and ``checked_at`` on a latest row is when that status began (or
the last manual check), not the last time anything looked. Rows outlive the
deployment they describe; :func:`latest_rows` marks those ``live: false``.

Secrets: ``error_message`` is the upstream exception text, which can quote a
backend URL (with a key in its path or query) or echo part of an API key.
:func:`redact_error` cuts URLs to their origin and masks key-shaped tokens.
"""

from __future__ import annotations

import re
from typing import Any

from cli_anything.litellm.core.mcp import url_origin

STATUSES = ("healthy", "unhealthy")

#: ``health_check_history_endpoint``'s ``limit`` bound (``ge=1, le=1000``).
HISTORY_MAX = 1000

MASK = "****"
_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")
_KEYISH = re.compile(
    r"\b(?:sk|pk|rk|hf|gsk|xai|key|tok|ghp|gho|glpat|AIza)[-_][A-Za-z0-9_\-]{6,}"
    r"|\bBearer\s+[A-Za-z0-9._\-]{8,}",
)
ERROR_MAX = 300


def redact_error(text: Any, limit: int = ERROR_MAX) -> str | None:
    """An upstream error that is safe to print: URLs → origin, key-like tokens masked."""
    if text is None or text == "":
        return None
    s = _URL.sub(lambda m: url_origin(m.group(0).rstrip(".,;:)")) or MASK, str(text))
    s = _KEYISH.sub(MASK, s)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def as_rows(res: Any, key: str) -> list[dict[str, Any]]:
    """Rows out of ``{key: [...]}`` or ``{key: {id: row}}`` (and a bare list)."""
    if isinstance(res, dict):
        res = res.get(key)
    if isinstance(res, dict):
        res = list(res.values())
    return [r for r in res if isinstance(r, dict)] if isinstance(res, list) else []


def normalize(row: dict[str, Any]) -> dict[str, Any]:
    """One printable row; ``details`` (unused by the proxy so far) is left out."""
    return {
        "model_name": row.get("model_name"),
        "model_id": row.get("model_id"),
        "status": row.get("status"),
        "healthy_count": row.get("healthy_count"),
        "unhealthy_count": row.get("unhealthy_count"),
        "response_time_ms": round(row["response_time_ms"], 1)
        if isinstance(row.get("response_time_ms"), (int, float))
        else None,
        "checked_at": row.get("checked_at"),
        "checked_by": row.get("checked_by"),
        "error": redact_error(row.get("error_message")),
    }


def latest_rows(res: Any, deployments: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """``/health/latest`` joined to the live deployments.

    Each recorded row gains ``live`` (its deployment still exists) and
    ``blocked`` (an admin paused it); every live deployment with no record at
    all is added as ``status: "unknown"``. Unhealthy first, then by model.
    """
    deployments = deployments or []
    live_ids = {(d.get("model_info") or {}).get("id"): d for d in deployments}
    live_names = {d.get("model_name") for d in deployments}
    rows, seen_ids, seen_names = [], set(), set()
    for r in as_rows(res, "latest_health_checks"):
        n = normalize(r)
        dep = live_ids.get(n["model_id"]) if n["model_id"] else None
        n["live"] = dep is not None if n["model_id"] else n["model_name"] in live_names
        n["blocked"] = bool((dep or {}).get("model_info", {}).get("blocked")) if dep else None
        rows.append(n)
        seen_ids.add(n["model_id"])
        if not n["model_id"]:
            seen_names.add(n["model_name"])
    for dep_id, d in live_ids.items():
        if dep_id in seen_ids or (d.get("model_name") in seen_names):
            continue
        rows.append(
            {
                **normalize({"model_name": d.get("model_name"), "model_id": dep_id}),
                "status": "unknown",
                "live": True,
                "blocked": bool((d.get("model_info") or {}).get("blocked")),
            }
        )
    order = {"unhealthy": 0, "unknown": 1, "healthy": 2}
    return sorted(rows, key=lambda x: (order.get(x["status"], 1), str(x["model_name"]), str(x["model_id"])))


def history_params(model: str | None, status: str | None, limit: int, offset: int) -> dict[str, Any]:
    """Query for ``/health/history``. Raises ``ValueError`` where the proxy would 422."""
    if not 1 <= limit <= HISTORY_MAX:
        raise ValueError(f"--limit must be 1..{HISTORY_MAX} (the proxy's bound)")
    if offset < 0:
        raise ValueError("--offset must be 0 or more")
    if status and status not in STATUSES:
        raise ValueError(f"--status must be one of {', '.join(STATUSES)}")
    p: dict[str, Any] = {"limit": limit, "offset": offset}
    if model:
        p["model"] = model
    if status:
        p["status_filter"] = status
    return p
