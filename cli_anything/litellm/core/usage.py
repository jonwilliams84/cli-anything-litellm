"""Server-side spend reports: the proxy aggregates its own log (/global/spend/report).

``spend`` and ``logs summary`` download every raw row in the window and
aggregate them client-side — exact and shape-tolerant, but a 90-day window on
a busy proxy means moving (and holding) thousands of rows to produce a few
numbers. The proxy keeps the same spend log in its database and aggregates it
server-side — the numbers behind the Admin UI's usage charts — and
``/global/spend/report?group_by=…`` reads that, so a wide money view is one
small response instead of a log download.

The shape is less rich than the raw log, and that is documented rather than
worked around:

- A row only carries what the chosen ``group_by`` puts on it: the group label,
  the day, spend and token totals. There is no per-request drill-down from
  here (use ``logs list`` in a narrow window for that).
- The group ids are the proxy's own. An ``api_key`` row is the log's
  sha256(key) hash — it is never a secret, but it is nameless without the key
  list, so callers name it exactly the way ``spend --by key`` does: through
  the live key list, the alias recorded at request time, or ``(master key)``
  for the key we authenticate with.
- ``group_by=day`` has no server-side form (the proxy always groups by an
  entity within the day), so it is the report's rows summed client-side into
  one row per day — still one response.

Envelope and number-type variants observed in the wild are tolerated in
:func:`normalize`, like every other reader in this package.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

DIMENSIONS = ("day", "team", "api_key", "model", "customer")

# CLI --by -> the group_by value the proxy understands (no group_by=day).
PROXY_GROUP: dict[str, str] = {
    "team": "team",
    "api_key": "api_key",
    "model": "model",
    "customer": "customer",
}

# Where the report may carry the group label on a row, per dimension.
_GROUP_FIELDS: dict[str, tuple[str, ...]] = {
    "team": ("team_id", "team_name", "team_alias"),
    "api_key": ("api_key_alias", "key_alias", "api_key", "key"),
    "model": ("model", "model_group"),
    "customer": ("customer", "end_user", "user"),
}

_HASH = re.compile(r"[0-9a-f]{64}")


def window(days: int = 7, start: str | None = None, end: str | None = None) -> dict[str, str]:
    """The query window.

    Explicit ``start``/``end`` (YYYY-MM-DD) for the wide window the raw-log
    commands cannot download; otherwise the last ``days`` inclusive, ending
    tomorrow — the same window ``spend`` uses, because the proxy's date
    filters are UTC days and fresh rows land in the log late.

    Raises ValueError (message meant for Click) on a one-sided range, a bad
    date, or an inverted range.
    """
    if not start and not end:
        e = date.today() + timedelta(days=1)
        return {"start_date": (e - timedelta(days=days + 1)).isoformat(), "end_date": e.isoformat()}
    if bool(start) != bool(end):
        raise ValueError("--start and --end go together")
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as ex:
        raise ValueError(f"--start/--end must be YYYY-MM-DD dates: {ex}") from ex
    if s > e:
        raise ValueError(f"--start {start} is after --end {end}")
    return {"start_date": s.isoformat(), "end_date": e.isoformat()}


def params(win: dict[str, str], group_by: str) -> dict[str, str]:
    """The GET ``/global/spend/report`` query for the window and dimension.

    ``group_by=day`` has no server-side form, so it asks for the default
    (team) rows and lets :func:`normalize` sum them into days.
    """
    out = {**win}
    if group_by in PROXY_GROUP:
        out["group_by"] = PROXY_GROUP[group_by]
    return out


def as_rows(res: Any) -> list[dict[str, Any]]:
    """Whatever ``/global/spend/report`` returned, as a list of report rows.

    The proxy has sent a bare list and a ``{"data": [...]}`` envelope across
    versions; anything else is treated as no rows rather than a crash.
    """
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for key in ("data", "daily", "rows"):
            if isinstance(res.get(key), list):
                return res[key]
    return []


def _num(v: Any) -> float:
    """Numeric fields arrive as real numbers, JSON strings, or nulls."""
    try:
        return round(float(v), 4) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _label(raw: dict[str, Any], group_by: str, names: dict[str, str]) -> str | None:
    """The row's group label per ``group_by``, named the way ``spend`` does.

    Only aliases and recorded aliases replace a hash: an unknown value that
    is not a sha256 hash is shown as-is (it may be the proxy's own marker
    such as ``aggregate_from_team_keys``), a hash nobody names becomes
    ``(unknown hh…)``.
    """
    for f in _GROUP_FIELDS[group_by]:
        v = raw.get(f)
        if v:
            v = str(v)
            if group_by == "api_key":
                return names.get(v) or f"(unknown {v[:10]}...)" if _HASH.fullmatch(v) else v
            return v
    return None


def normalize(
    rows: list[dict[str, Any]],
    group_by: str = "team",
    names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Report rows in the CLI's money-view shape: date, ``group_by``,
    spend, prompt/completion tokens.

    For ``group_by=day`` the entity-bearing rows are summed client-side into
    one row per day. A row that names no group (the shape varies) is reported
    under ``(unattributed)`` rather than dropped.
    """
    names = names or {}
    if group_by == "day":
        agg: dict[str, dict[str, Any]] = {}
        for raw in rows or []:
            date_str = str(raw.get("date") or "")[:10]
            a = agg.setdefault(
                date_str,
                {"date": date_str, "spend": 0.0, "prompt_tokens": 0, "completion_tokens": 0},
            )
            a["spend"] += _num(raw.get("spend"))
            a["prompt_tokens"] += round(_num(raw.get("prompt_tokens")))
            a["completion_tokens"] += round(_num(raw.get("completion_tokens")))
        for a in agg.values():
            a["spend"] = round(a["spend"], 4)
        out = sorted(agg.values(), key=lambda r: r["date"])
    else:
        out = []
        for raw in rows or []:
            out.append(
                {
                    "date": str(raw.get("date") or "")[:10],
                    group_by: (_label(raw, group_by, names) or "(unattributed)"),
                    "spend": _num(raw.get("spend")),
                    "prompt_tokens": round(_num(raw.get("prompt_tokens"))),
                    "completion_tokens": round(_num(raw.get("completion_tokens"))),
                }
            )
        out = sorted(out, key=lambda r: (r["date"], r["spend"]), reverse=True)
    return out


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The window's roll-up, whatever group_by produced the rows."""
    return {
        "rows": len(rows),
        "days": len({r.get("date") for r in rows if r.get("date")}),
        "spend": round(sum(_num(r.get("spend")) for r in rows), 4),
        "prompt_tokens": round(sum(_num(r.get("prompt_tokens")) for r in rows)),
        "completion_tokens": round(sum(_num(r.get("completion_tokens")) for r in rows)),
    }
