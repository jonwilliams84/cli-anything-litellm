"""Per-request logs from the proxy's spend log (``GET /spend/logs/v2``, paged; see ``litellm_cli._fetch_logs``).

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
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

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
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # The proxy writes naive UTC; left naive, astimezone() would read it as local time.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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


MASTER_KEY_LITERAL = "litellm_proxy_master_key"


def key_alias_map(keys: list[dict[str, Any]], auth_key: str | None = None) -> dict[str, str]:
    """Hashed token → alias, for naming callers the way ``spend --by key`` does.

    Includes the key we authenticate with (its hash is how the proxy records
    the master key's own calls) and tolerates keys without a token.
    """
    out = {k["token"]: k.get("key_alias") or k.get("key_name") for k in keys if k.get("token")}
    if auth_key:
        out[hashlib.sha256(auth_key.encode()).hexdigest()] = "(master key)"
    # Newer proxies (seen on v1.106.3) log the master key's calls under this literal instead.
    out[MASTER_KEY_LITERAL] = "(master key)"
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


# ── one request, whole trace ────────────────────────────────────────────────
#
# The paged listing (``GET /spend/logs/v2``, what ``logs list`` reads) leaves
# out the ``request``/``response`` columns — the heavy ones that OOM-killed
# the unpaged route. The proxy serves one request's full row (payloads
# included, the columns the Admin UI's request drawer shows) on
# ``GET /spend/log/{request_id}``. This is the "why was I billed for this /
# what exactly did this call send" view; payloads can carry secrets, so they
# are redacted before they reach the CLI's output.

#: Payload field names whose values are masked, whatever they claim to carry:
#: the auth headers the proxy stores verbatim inside the request payload, and
#: credential-named fields elsewhere (bare ``key`` is *not* here — model
#: groups and tag rows use it for non-secret values).
SECRET_SEGMENTS = (
    "authorization",
    "api_key",
    "token",
    "secret",
    "password",
    "credential",
    "cookie",
    "bearer",
)

PAYLOAD_MASK = "****"

#: An ``sk-…`` token seen anywhere in a payload string is masked: the request
#: column can carry the caller key verbatim in text fields.
sk_token = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def detail_path(request_id: str) -> str:
    """The ``GET /spend/log/…`` path for one request.

    The id is quoted into the path segment (a proxy that reads it bare would
    route ``…/log/a/b`` elsewhere), and an empty id is refused before any call.
    """
    if not (request_id or "").strip():
        raise ValueError("no request_id — the log is keyed by one; `logs list` shows what is there")
    return "/spend/log/" + quote(request_id.strip(), safe="")


def unwrap(res: Any) -> dict[str, Any] | None:
    """The row out of ``GET /spend/log``'s answer, tolerant of the shapes
    seen across proxy versions: a bare row, ``{"result": …}`` / ``{"log": …}``
    / ``{"data": …}``, or a one-element list."""
    if isinstance(res, list):
        return res[0] if res else None
    if isinstance(res, dict):
        for k in ("result", "log", "data"):
            v = res.get(k)
            if isinstance(v, list):
                return v[0] if v else None
            if isinstance(v, dict):
                return v
        return res
    return None


def _secret_name(k: Any) -> bool:
    if not isinstance(k, str):
        return False
    name = k.lower().replace("-", "_").replace("_", "")
    return any(seg.replace("_", "") in name for seg in SECRET_SEGMENTS)


def redact(value: Any) -> Any:
    """A copy of a payload safe to print: values under credential-named keys
    are masked whole (an ``os.environ/NAME`` reference names a variable, not a
    secret, and stays), any other ``sk-…`` token in a string is masked in
    place, and everything else passes untouched."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _secret_name(k) and isinstance(v, str) and v and not v.startswith("os.environ/"):
                out[k] = PAYLOAD_MASK
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return sk_token.sub(PAYLOAD_MASK, value)
    return value


def payload(row: dict[str, Any], name: str) -> Any:
    """One payload column, parsed (the proxy stores it as a JSON *string*)
    and redacted. Unparsable text is kept as a string — with tokens scrubbed;
    a missing/empty column is ``None``."""
    v = row.get(name)
    if v in (None, ""):
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return redact(v)
    return redact(v)


def detail(row: dict[str, Any], aliases: dict[str, str] | None = None) -> dict[str, Any]:
    """One request, whole trace: the ``normalize`` row plus what the listing
    leaves out — the end time, the call's recorded status and the redacted
    ``request``/``response`` payloads."""
    end = _dt(row.get("endTime", row.get("end_time")))
    out = normalize(row, aliases)
    out["end_time"] = end.astimezone(timezone.utc).isoformat(timespec="seconds") if end else None
    meta = row.get("metadata") or {}
    out["status"] = row.get("status") or meta.get("status")
    out["request"] = payload(row, "request")
    out["response"] = payload(row, "response")
    return out
