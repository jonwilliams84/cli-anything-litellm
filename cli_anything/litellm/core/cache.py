"""The proxy's response cache: health, contents, settings and invalidation.

LiteLLM fronts an LLM *response cache* (``litellm.cache``, usually Redis)
that stores completions for a TTL so repeated prompts are served without
hitting (and paying) the backend. v1.106.3 serves the whole family from
``litellm/proxy/caching_routes.py`` and
``litellm/proxy/management_endpoints/cache_settings_endpoints.py``:

- ``GET /cache/ping`` (``cache_ping``): ``CachePingResponse`` — status,
  cache_type, and for a redis cache ``ping_response``/``set_cache_response``
  (it PINGs **and writes a test entry**, ``async_add_cache``, so it is not a
  pure read), ``litellm_cache_params`` — a JSON *string* of the masked
  ``vars(litellm.cache)`` — and ``health_check_cache_params``, the masked
  ``vars`` of the backend cache filtered to host / port / redis_kwargs /
  namespace / redis_version (``HealthCheckCacheParams``). No cache → 503
  ``ProxyException`` (``{"error": {message, type: cache_ping_error, ...}}``).
- ``GET /cache/redis/info`` (``cache_redis_info``): ``num_clients`` (-1 when
  CLIENT LIST is restricted, common on managed Redis), ``clients`` and the
  full Redis ``INFO`` dict. Every failure — including the handler's own
  inner HTTPExceptions (no cache, non-redis type) — lands in its
  ``except Exception`` and surfaces as 503 ``Service Unhealthy (<inner>)``.
- ``POST /cache/delete`` (``cache_delete``): body ``{"keys": [...]}`` — the
  cache keys every proxied response carries in its ``x-litellm-cache-key``
  header (``proxy/common_request_processing.py``). Redis only; any failure
  is rewrapped as 500 ``Cache Delete Failed(<inner>)``.
- ``POST /cache/flushall`` (``cache_flushall``): drops EVERY cached response
  (the next request per prompt re-hits its backend). Same 503 wrapping as
  /redis/info.
- ``GET /cache/settings`` (``get_cache_settings``): ``fields`` metadata
  (``CACHE_SETTINGS_FIELDS``, ``types/management_endpoints/
  cache_settings_endpoints.py``), ``current_values`` and
  ``redis_type_descriptions``. current_values is the stored row's decrypted
  settings overlaid with the REDIS_* environment the cache itself reads
  (``_overlay_environment``; stored values win), with url precedence applied
  (``_resolve_cache_url_precedence``: a ``url`` drops host/port/db/password/
  username) and ``redis_type`` derived for the UI (node | cluster | sentinel,
  from the node lists). Credential fields — ``password``, ``sentinel_password``,
  ``url`` and anything the proxy's ``SensitiveDataMasker`` flags — come back as
  ``***REDACTED***`` (``_redact_credentials``).
- ``POST /cache/settings/test`` (``test_cache_connection``):
  ``CacheTestResponse`` ``{status: success|failed, message, error}``, always
  HTTP 200 — the test connects with the given settings and never saves.
  Credentials echoed back as the redaction marker are restored from the
  stored row for the test (``_merge_over_saved``).
- ``POST /cache/settings`` (``update_cache_settings``): needs a database and
  ``STORE_MODEL_IN_DB`` (else 500). REPLACES the whole settings map (only a
  credential omitted or echoed as the marker is preserved —
  ``_merge_over_saved``), re-initialises the cache on the answering worker,
  switches response caching on, writes an audit row and returns the redacted
  settings.

The traps this module encodes:

- **The DB row beats config.yaml.** ``init_cache_settings_in_db`` re-applies
  the stored row at every startup, so once a row exists it shadows
  ``cache_settings`` in config.yaml — the *reverse* of the fallback lists'
  ownership. Saving while the effective settings are empty (no row, no
  REDIS_* env) pins a partial row over a config-owned cache, so the CLI
  gates that save (:func:`empty_save`).
- **Anti-replay.** A stored credential belongs to the stored connection
  target (``_saved_secret_is_reusable`` over host / port / url /
  ``redis_startup_nodes`` / ``sentinel_nodes`` / ``service_name``): change the
  target without supplying a new credential and the proxy DROPS the stored one
  rather than replaying it at the new target — and a supplied ``url`` wins
  over a stored one while a discrete target (host / nodes) drops a stored url
  (``_merge_over_saved``). :func:`dropped_credentials` names what a save will
  drop, client-side.
- **Secrets never ride argv.** ``password`` / ``sentinel_password`` and a
  url carrying userinfo (``redis://:pass@host``) must be ``os.environ/NAME``
  references — the redis client resolves them at connect time
  (``_get_redis_client_logic``), so the secret stays out of the proxy's DB,
  argv history and this CLI's output.
- **The settings view is not the live cache.** ``GET /cache/settings`` is the
  DB row overlaid with the REDIS_* environment. A proxy whose cache comes
  from config.yaml (``cache_params.host``) and whose REDIS_* env names a
  *different* Redis (the usage / rate-limit store, ``coordination_redis``)
  reports that other Redis here. Saving it would move the response cache onto
  the usage store, where LRU eviction of cached answers pushes out rate-limit
  and budget counters, and the row would beat config.yaml at every restart.
  :func:`target_mismatch` compares the target a save would pin with the live
  backend ``/cache/ping`` reports (seen on the house proxy, 2026-10-25:
  settings said litellm-redis, the live cache was litellm-cache).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from cli_anything.litellm.core.mcp import url_origin

#: The proxy's own redaction marker (``_REDACTED_VALUE``): GET /cache/settings
#: shows it for every credential field, and POST maps it back to the stored
#: secret instead of persisting the marker.
REDACTED = "***REDACTED***"

MASK = "****"

#: Credential fields the proxy always redacts on read
#: (``_CACHE_SENSITIVE_FIELDS``). ``url`` counts: a redis URL can embed the
#: password inline.
PROXY_REDACTED_FIELDS = ("password", "sentinel_password", "url")

#: Fields that identify which Redis a stored credential belongs to
#: (``_CONNECTION_TARGET_FIELDS``): changing one makes the proxy drop the
#: stored credential instead of replaying it at the new target.
TARGET_FIELDS = ("host", "port", "url", "redis_startup_nodes", "sentinel_nodes", "service_name")

#: The sensitive key segments of the proxy's ``SensitiveDataMasker``
#: (``_DEFAULT_SENSITIVE_PATTERNS``) — a value under any of these keys never
#: reaches the output unmasked.
SENSITIVE_SEGMENTS = frozenset(
    (
        "password",
        "secret",
        "key",
        "token",
        "auth",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "access",
        "private",
        "certificate",
        "fingerprint",
        "tenancy",
    )
)

#: ``--set`` field types, from ``CACHE_SETTINGS_FIELDS`` (``field_type``):
#: Integer, Float, Boolean and List fields are typed; everything else is a
#: string. A bad type is refused here, not by the proxy's runtime.
FIELD_TYPES: dict[str, type] = {
    "db": int,
    "port": int,
    "max_connections": int,
    "ttl": float,
    "similarity_threshold": float,
    "ssl": bool,
    "ssl_check_hostname": bool,
    "aws_iam_auth": bool,
    "aws_iam_serverless": bool,
    "redis_startup_nodes": list,
    "sentinel_nodes": list,
}

#: UI-only: ``get_cache_settings`` derives it from the node lists (node |
#: cluster | sentinel) and cache init drops it — setting it changes nothing.
DERIVED_FIELDS = ("redis_type",)


def _is_sensitive(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    return any(seg in SENSITIVE_SEGMENTS for seg in key.lower().replace("-", "_").split("_"))


def redact_params(params: Any) -> Any:
    """A settings/params tree safe to print: credential keys fully masked, urls cut to origin.

    The proxy already masks its own outputs (``masker.mask_dict`` keeps four
    real characters; ``_redact_credentials`` swaps credentials for the
    marker), but this runs over every value the CLI echoes so a masked-once
    path or a future field cannot leak a secret.
    """
    if isinstance(params, dict):
        out: dict[str, Any] = {}
        for k, v in params.items():
            if _is_sensitive(k) and v not in (None, "", REDACTED):
                out[k] = MASK
            elif k == "url" and isinstance(v, str) and v != REDACTED:
                out[k] = url_origin(v) or MASK
            else:
                out[k] = redact_params(v)
        return out
    if isinstance(params, list):
        return [redact_params(v) for v in params]
    return params


def parse_json_object(value: Any) -> dict[str, Any]:
    """``litellm_cache_params`` arrives as a JSON *string* (``safe_dumps``);
    tolerating a dict and an unparseable string costs nothing."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def ping_row(res: Any) -> dict[str, Any]:
    """One ``CachePingResponse``, normalised: the JSON-string params parsed
    and redacted, ``ping_response``/``set_cache_response`` kept as-is (absent
    for a non-redis cache)."""
    res = res or {}
    return {
        "status": res.get("status"),
        "cache_type": res.get("cache_type"),
        "ping_response": res.get("ping_response"),
        "set_cache_response": res.get("set_cache_response"),
        "params": redact_params(parse_json_object(res.get("litellm_cache_params"))),
        "health_check_params": redact_params(res.get("health_check_cache_params") or {}),
    }


def _num(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    try:
        f = float(value)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return value


def info_summary(res: Any) -> dict[str, Any]:
    """The operator view of ``GET /cache/redis/info``: the Redis the response
    cache runs on — version, memory, hit rate, evictions, total keys (summed
    over every ``dbN`` keyspace). Raw INFO spellings (``*_human`` fields)
    are kept so the numbers match ``redis-cli INFO``."""
    res = res or {}
    info = res.get("info") or {}
    hits = _num(info.get("keyspace_hits"))
    misses = _num(info.get("keyspace_misses"))
    total_keys = 0
    for k, v in info.items():
        if k.startswith("db") and isinstance(v, dict):
            total_keys += int(v.get("keys") or 0)
    try:
        rate = (
            round(100 * hits / (hits + misses))
            if hits and isinstance(hits, (int, float))
            else (None if not (hits or misses) else 0)
        )
    except (TypeError, ZeroDivisionError):
        rate = None
    return {
        "redis_version": info.get("redis_version"),
        "uptime_in_days": info.get("uptime_in_days"),
        "used_memory": info.get("used_memory_human"),
        "used_memory_peak": info.get("used_memory_peak_human"),
        "maxmemory": info.get("maxmemory_human"),
        "maxmemory_policy": info.get("maxmemory_policy"),
        "total_keys": total_keys,
        "keyspace_hits": hits,
        "keyspace_misses": misses,
        "hit_rate_percent": rate,
        "evicted_keys": info.get("evicted_keys"),
        "expired_keys": info.get("expired_keys"),
        "ops_per_sec": info.get("instantaneous_ops_per_sec"),
        "connected_clients": info.get("connected_clients"),
        "num_clients": res.get("num_clients"),
    }


def info_row(res: Any) -> dict[str, Any]:
    """The full ``/cache/redis/info`` response plus the computed summary."""
    res = res or {}
    return {
        "num_clients": res.get("num_clients"),
        "clients": res.get("clients"),
        "info": res.get("info"),
        "summary": info_summary(res),
    }


def settings_row(res: Any) -> dict[str, Any]:
    """``GET /cache/settings`` for the CLI: the redacted ``current_values``
    (what the proxy would re-save), field metadata and the redis type
    descriptions."""
    res = res or {}
    return {
        "current_values": redact_params(res.get("current_values") or {}),
        "redis_type_descriptions": res.get("redis_type_descriptions") or {},
        "fields": res.get("fields") or [],
    }


def _has_target(value: Any) -> bool:
    """``_has_connection_target``: a live discrete connection target."""
    if isinstance(value, str):
        return value.strip() != "" and value != REDACTED
    return value not in (None, [], {})


def _repr(value: Any) -> str:
    """``_target_repr``: canonical string form for target equality."""
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def _secret_reusable(incoming: dict[str, Any], saved: dict[str, Any]) -> bool:
    """``_saved_secret_is_reusable`` over the visible (redacted) settings: a
    stored credential is kept only when every target field the stored config
    pins still matches. ``url`` is the exception — redacted or omitted means
    "keep the stored url", only a different supplied url blocks reuse."""
    for field in TARGET_FIELDS:
        if field == "url":
            incoming_url = incoming.get("url")
            if incoming_url in (None, "", REDACTED):
                continue
            if _repr(incoming_url) != _repr(saved.get("url")):
                return False
            continue
        if saved.get(field) in (None, "", [], {}):
            continue  # the stored config does not pin this dimension
        if _repr(incoming.get(field)) != _repr(saved.get(field)):
            return False
    return True


def dropped_credentials(body: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Credentials the proxy will DROP on this save (``_merge_over_saved``).

    A redacted marker in ``current`` means a stored secret exists
    (``_redact_credentials`` only redacts non-None values). The proxy keeps it
    only when the connection target is unchanged — otherwise the credential is
    popped, never replayed at a target it does not belong to. A discrete
    target (host / cluster / sentinel nodes) in the body also drops a stored
    ``url`` (url precedence would otherwise resurrect it over the new host).
    """
    cs = body.get("cache_settings") or {}
    current = current or {}
    out: list[str] = []
    reusable = _secret_reusable(cs, current)
    switching_discrete = any(
        _has_target(cs.get(f)) for f in ("host", "redis_startup_nodes", "sentinel_nodes")
    )
    for cred in ("password", "sentinel_password"):
        if current.get(cred) == REDACTED and cs.get(cred) in (None, REDACTED) and not reusable:
            out.append(cred)
    if current.get("url") == REDACTED and cs.get("url") in (None, "", REDACTED) and switching_discrete:
        out.append("url")
    return sorted(out)


def empty_save(current: dict[str, Any], sets: dict[str, Any]) -> bool:
    """True when this save pins settings over a config-owned cache.

    Empty ``current_values`` means no database row and no REDIS_* environment:
    the cache (if any) comes from config.yaml, and once a row exists the row
    beats the config at every restart (``init_cache_settings_in_db``). Saving
    a partial row in that state silently shadows the config's cache_settings.
    """
    live = {
        k: v for k, v in (current or {}).items() if k not in DERIVED_FIELDS and v not in (None, "", [], {})
    }
    return not live and bool(sets)


def _refuse_literal_secret(name: str, value: str) -> None:
    if value.startswith("os.environ/"):
        return
    raise ValueError(
        f"--set {name}: pass secrets as os.environ/NAME (the proxy's redis client resolves "
        "the variable at connect time), never literally"
    )


def parse_sets(pairs: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """``--set key=value`` pairs, typed per ``CACHE_SETTINGS_FIELDS``.

    Refused before the proxy is called: a missing ``=``, the UI-only
    ``redis_type`` (derived from the node lists; cache init ignores it), a
    literal ``password``/``sentinel_password`` or a url carrying userinfo
    (``redis://:pass@host``) — all must be ``os.environ/NAME`` — and values
    the declared field type cannot parse.
    """
    out: dict[str, Any] = {}
    for p in pairs:
        if "=" not in p:
            raise ValueError(f"--set takes key=value (got {p!r})")
        k, v = p.split("=", 1)
        k = k.strip()
        if not k:
            raise ValueError(f"--set takes key=value (got {p!r})")
        if k in DERIVED_FIELDS:
            raise ValueError(
                f"--set {k}: the proxy derives it for the UI (node|cluster|sentinel, from the node "
                "lists) and cache init ignores it — set redis_startup_nodes / sentinel_nodes instead"
            )
        kind = FIELD_TYPES.get(k)
        if k in ("password", "sentinel_password"):
            _refuse_literal_secret(k, v)
            out[k] = v
            continue
        if k == "url":
            try:
                netloc = urlsplit(v).netloc if not v.startswith("os.environ/") else ""
            except ValueError:
                netloc = ""
            if "@" in netloc:
                _refuse_literal_secret(k, v)
            out[k] = v
            continue
        if kind is bool:
            if v.lower() not in ("true", "false"):
                raise ValueError(f"--set {k} is Boolean: true or false (got {v!r})")
            out[k] = v.lower() == "true"
        elif kind is int:
            try:
                out[k] = int(v)
            except ValueError as e:
                raise ValueError(f"--set {k} is Integer (got {v!r})") from e
        elif kind is float:
            try:
                out[k] = float(v)
            except ValueError as e:
                raise ValueError(f"--set {k} is Float (got {v!r})") from e
        elif kind is list:
            try:
                parsed = json.loads(v)
            except ValueError as e:
                raise ValueError(
                    f'--set {k} is List: a JSON array, e.g. \'[{{"host": "h"}}]\' (got {v!r})'
                ) from e
            if not isinstance(parsed, list):
                raise ValueError(f"--set {k} is List: a JSON array (got {v!r})")
            out[k] = parsed
        else:
            out[k] = v
    return out


def build_body(current: dict[str, Any], sets: dict[str, Any]) -> dict[str, Any]:
    """The ``POST /cache/settings`` (and ``/cache/settings/test``) body.

    The endpoint REPLACES the whole settings map — only a credential omitted
    or echoed as the redaction marker survives (``_merge_over_saved``) — so a
    change of one field must still send every other field the proxy reports,
    markers included (the proxy maps them back to the stored secrets). The
    UI-only ``redis_type`` is left out: cache init drops it anyway.
    """
    merged = {k: v for k, v in (current or {}).items() if k not in DERIVED_FIELDS}
    merged.update(sets)
    return {"cache_settings": merged}


def delete_body(keys: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """The ``POST /cache/delete`` body. ``cache_delete`` reads ``keys`` with a
    default of None and the redis client then fails iterating it, so at least
    one key is required up front."""
    ks = [k.strip() for k in keys if k and k.strip()]
    if not ks:
        raise ValueError("at least one cache key is required (the x-litellm-cache-key header of a response)")
    return {"keys": ks}


def live_target(ping: dict[str, Any]) -> dict[str, str]:
    """The Redis the live cache really uses, from a :func:`ping_row`: a RedisCache
    keeps host/port inside ``redis_kwargs`` (``health_check_cache_params`` has
    them as None — or, on the live v1.106.3 proxy, as the string "None").
    Empty for a non-redis cache or a ping that carries neither."""
    hc = (ping or {}).get("health_check_params") or {}
    kw = hc.get("redis_kwargs") if isinstance(hc.get("redis_kwargs"), dict) else {}
    absent = (None, "", "None")  # the live v1.106.3 proxy sends unset fields as the string "None"
    out = {}
    for field in ("host", "port"):
        value = hc.get(field) if hc.get(field) not in absent else kw.get(field)
        if value not in absent:
            out[field] = str(value)
    return out


def target_mismatch(settings: dict[str, Any], ping: dict[str, Any]) -> str | None:
    """Why the settings' connection target is not the live cache, or None.

    Compares host and port only, and only when both sides name one: a url
    (redacted) or a cluster/sentinel topology can't be compared from here.
    """
    live = live_target(ping)
    want = {f: str(settings[f]) for f in ("host", "port") if settings.get(f) not in (None, "", REDACTED)}
    if not live.get("host") or not want.get("host"):
        return None
    if want["host"] != live["host"] or ("port" in want and "port" in live and want["port"] != live["port"]):
        here = ":".join(want[f] for f in ("host", "port") if f in want)
        there = ":".join(live[f] for f in ("host", "port") if f in live)
        return (
            f"the settings name {here}, but the live response cache is {there} (cache ping). "
            "GET /cache/settings is the DB row plus the REDIS_* environment, and REDIS_* can name "
            "another Redis (the usage/rate-limit store) while config.yaml points the cache elsewhere"
        )
    return None
