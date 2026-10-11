"""Per-request logs from the proxy's spend log (``GET /spend/logs``).

Every completion the proxy serves is attributed to a row in its spend log:
``request_id``, ``call_type``, the model and its group, the (hashed) key that
paid, the end user and team, token counts, spend, cache hit, and the request's
``startTime``/``endTime``. ``spend`` already aggregates those rows into totals
per model/key/team/user; this module exposes them one request at a time, which
is the "why is it slow / why did this key burn its budget" view.

The proxy writes the log asynchronously: right after traffic the newest
requests are not there yet. Filtering by window happens server-side
(``start_date``/``end_date``); the per-row filters (key, user, team, model)
are applied client-side, so they work even on proxies that silently ignore
the equivalent query parameters.

Row shape notes (tolerated variants, as different proxies emit them):

- timestamps: ``startTime``/``endTime`` (also ``start_time``/``end_time``),
  ISO-8601 strings or epoch seconds; failed calls may have no ``endTime``, the
  proxy's own ``ending_latency_s`` is used as a fallback for latency.
- the caller: ``api_key`` is the sha256 hash of the virtual key; the friendly
  ``key_alias`` is not always repeated in the row — older proxies hid it in
  ``metadata.user_api_key_alias``, which is how a later-deleted or rotated key
  still gets named.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

MODEL_FIELDS = ("model_group", "model")
TIME_FIELDS = ("startTime", "start_time")


def _dt(value: Any) -> datetime | None:
    """Parse a log timestamp: ISO-8601 string or epoch seconds. None if unusable."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 1e12 else value, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def started(row: dict[str, Any]) -> datetime | None:
    for f in TIME_FIELDS:
        if f in row:
            return _dt(row[f])
    return None


def latency_ms(row: dict[str, Any]) -> float | None:
    """Request round-trip in ms, from ``endTime - startTime``.

    A row the proxy failed mid-call has no ``endTime``; ``ending_latency_s``
    (the fraction of a second it survived) is the fallback. None if neither
    is usable — absent, not zero, so aggregates skip it.
    """
    t0, t1 = started(row), _dt(row.get("endTime", row.get("end_time")))
    if t0 and t1 and t1 > t0:
        return round((t1 - t0).total_seconds() * 1000, 1)
    if row.get("ending_latency_s") is not None:
        # a failed row has no endTime; the proxy's own "it survived this long" is better
        try:
            return round(float(row["ending_latency_s"]) * 1000, 1)
        except (TypeError, ValueError):
            return None
    if t0 and t1 and t1 == t0:
        return 0.0
    return None


def _caller(row: dict[str, Any], aliases: dict[str, str]) -> str:
    """Friendly name for the key that made the call, per ``spend --by key``."""
    token = row.get("api_key") or (row.get("metadata") or {}).get("user_api_key_hash")
    if token and token in aliases:
        return aliases[token]
    logged = (row.get("metadata") or {}).get("user_api_key_alias")
    if logged:
        return f"{logged} (gone)"  # named at request time; renamed, rotated or deleted since
    if token:
        return f"(unknown {token[:10]}...)" if re.fullmatch(r"[0-9a-f]{64}", str(token)) else "(unknown ...)"
    return None


def key_alias_map(keys: list[dict[str, Any]], auth_key: str | None = None) -> dict[str, str]:
    """Hashed token → alias, for naming callers the way ``spend --by key`` does.

    Includes the key we authenticate with (its hash is how the proxy records
    the master key's own calls) and tolerates keys without a token.
    """
    out = {k["token"]: k.get("key_alias") or k.get("key_name") for k in keys if k.get("token")}
    if auth_key:
        out[hashlib.sha256(auth_key.encode()).hexdigest()] = "(master key)"
    return out


def token_of(ref: str, keys: list[dict[str, Any]]) -> str:
    """What log rows record in ``api_key`` for a ``--key`` reference.

    A raw ``sk-…`` key is hashed (the proxy logs sha256(key), never the
    secret); an already-hashed token is passed through; an alias is resolved
    against the live key list and refused when it matches none or several.
    Raises ValueError with a message meant for ``keys list``.
    """
    ref = ref or ""
    if ref.startswith("sk-"):
        return hashlib.sha256(ref.encode()).hexdigest()
    if re.fullmatch(r"[0-9a-f]{64}", ref):
        return ref
    hits = [k for k in keys if k.get("key_alias") == ref]
    if not hits:
        raise ValueError(f"no key with alias {ref!r} — run `keys list`")
    if len(hits) > 1:
        raise ValueError(f"alias {ref!r} matches {len(hits)} keys — filter by token instead")
    token = hits[0].get("token")
    if not token:
        raise ValueError(f"alias {ref!r} has no token — filter by the raw sk-… key instead")
    return token


def filter_logs(
    rows: list[dict[str, Any]],
    key: str | None = None,
    user: str | None = None,
    team: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Client-side per-row filters, on top of the server-side date window."""
    out = []
    for r in rows:
        meta = r.get("metadata") or {}
        if key:
            token = r.get("api_key") or meta.get("user_api_key_hash")
            if token != key:
                continue
        if user and (r.get("user") or meta.get("user_api_key_user_id")) != user:
            continue
        if team and r.get("team_id") != team:
            continue
        if model:
            groups = {r.get(f) for f in MODEL_FIELDS if r.get(f)}
            if model not in groups:
                continue
        out.append(r)
    return out


def normalize(row: dict[str, Any], aliases: dict[str, str] | None = None) -> dict[str, Any]:
    """One log row, as both the table and --json output show it.

    The caller is named via ``aliases`` (see :func:`key_alias_map`): never the
    hash itself, which is a secret-shaped value.
    """
    prompt = row.get("prompt_tokens") or 0
    completion = row.get("completion_tokens") or 0
    total = row.get("total_tokens") or (prompt + completion)
    when = started(row)
    return {
        "request_id": row.get("request_id"),
        "when": when.astimezone(timezone.utc).isoformat(timespec="seconds") if when else None,
        "model": next((row[f] for f in MODEL_FIELDS if row.get(f)), None),
        "call_type": row.get("call_type"),
        "caller": _caller(row, aliases or {}),
        "user": row.get("user"),
        "team_id": row.get("team_id"),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "spend": round(row.get("spend") or 0.0, 6),
        "latency_ms": latency_ms(row),
        "cache_hit": row.get("cache_hit") or False,
    }


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    """Index of the (upper) element at ``pct``; None on no values."""
    if not sorted_values:
        return None
    i = max(0, min(len(sorted_values) - 1, round(pct / 100 * len(sorted_values)) - 1))
    return sorted_values[i]


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Traffic over the filtered rows: totals, latency percentiles, per-model breakdown.

    The money view is ``spend`` (grouped, per window); this is the traffic and
    latency view over the same log: what share of requests each model takes,
    whether any latency tail is forming, how often cache answered.
    """
    total_requests = 0
    total_spend = 0.0
    prompt = completion = cache_hits = 0
    latencies: list[float] = []
    by_model: dict[str, dict[str, Any]] = {}
    for r in rows:
        n = normalize(r)
        total_requests += 1
        total_spend += n["spend"]
        prompt += n["prompt_tokens"]
        completion += n["completion_tokens"]
        cache_hits += 1 if n["cache_hit"] else 0
        m = by_model.setdefault(n["model"] or "(rejected)", {"requests": 0, "spend": 0.0, "latency": []})
        m["requests"] += 1
        m["spend"] += n["spend"]
        if n["latency_ms"] is not None:
            latencies.append(n["latency_ms"])
            m["latency"].append(n["latency_ms"])
    latencies.sort()
    return {
        "requests": total_requests,
        "spend": round(total_spend, 4),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_hits": cache_hits,
        "latency_ms": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "max": _percentile(latencies, 100),
        },
        "models": [
            {
                "model": model,
                "requests": v["requests"],
                "spend": round(v["spend"], 4),
                "latency_ms": {
                    "p50": _percentile(sorted(v["latency"]), 50),
                    "p95": _percentile(sorted(v["latency"]), 95),
                },
            }
            for model, v in sorted(by_model.items(), key=lambda kv: (-kv[1]["spend"], kv[0]))
        ],
    }
