"""Response shapes of LiteLLM v1.106.3's response-cache endpoints, for offline tests.

Paths are relative to ``litellm/`` in the v1.106.3 tree. Every factory names
the function whose response it reproduces.

None of these could be field-checked against a live proxy: every ``/cache/*``
route is proxy-admin (``route_checks._raise_admin_only_route_exception``) and
the key this CLI runs as on the house estate is not one — the live v1.106.3
proxy answered 401 ``ADMIN_DENIED`` for both ``/cache/settings`` and
``/cache/redis/info``, which is the one live-captured shape here.
"""

from __future__ import annotations

import json
from typing import Any

REDIS_PASSWORD = "SuperSecretRedisPass"


def proxy_mask(value: str) -> str:
    """``SensitiveDataMasker.mask_value`` (``litellm_core_utils/
    sensitive_data_masker.py``) with its defaults: first four and last four
    characters visible, the middle masked — what ``masker.mask_dict`` leaves
    of a credential in every ``/cache/ping`` response."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _cache_vars() -> dict[str, Any]:
    """``vars(litellm.cache)`` for a redis cache (``caching/caching.py``,
    ``Cache.__init__`` sets type / namespace / ttl / mode /
    supported_call_types / redis_flush_size / semantic_cache_scope /
    max_messages — the connection lives on the backend, not the facade)."""
    return {
        "supported_call_types": ["aspeech", "atext_completion", "acompletion", "aembedding"],
        "type": "redis",
        "namespace": "litellm_proxy",
        "redis_flush_size": 100,
        "ttl": 300.0,
        "mode": "default_on",
        "semantic_cache_scope": "key",
        "max_messages": 4,
    }


def _redis_kwargs(masked: bool = True) -> dict[str, Any]:
    """``RedisCache.redis_kwargs`` (``caching/redis_cache.py``): host, port and
    the credential ride together; ``mask_dict`` keeps 4+4 of the password."""
    return {
        "host": "redis.litellm.svc",
        "port": "6379",
        "db": 0,
        "password": proxy_mask(REDIS_PASSWORD) if masked else REDIS_PASSWORD,
        "socket_timeout": 5.0,
    }


def ping_redis(masked_password: bool = True) -> dict[str, Any]:
    """``CachePingResponse`` from ``cache_ping`` (``proxy/caching_routes.py``),
    the redis branch: PING ok and the test entry written. ``litellm_cache_params``
    is a JSON *string* (``safe_dumps`` of the masked vars, with the
    self-referencing ``cache`` key popped); ``health_check_cache_params`` is
    ``HealthCheckCacheParams`` over the backend's vars — host/port are None
    there because a RedisCache keeps them inside ``redis_kwargs``."""
    return {
        "status": "healthy",
        "cache_type": "redis",
        "ping_response": True,
        "set_cache_response": "success",
        "litellm_cache_params": json.dumps(_cache_vars()),
        "health_check_cache_params": {
            "host": None,
            "port": None,
            "redis_kwargs": _redis_kwargs(masked_password),
            "namespace": "litellm_proxy",
            "redis_version": "7.2.4",
        },
    }


def ping_local() -> dict[str, Any]:
    """``cache_ping`` for a non-redis cache: no ``ping_response`` /
    ``set_cache_response`` keys at all, and the backend params filter to None
    (an InMemoryCache has none of the five fields)."""
    return {
        "status": "healthy",
        "cache_type": "local",
        "litellm_cache_params": json.dumps({**_cache_vars(), "type": "local", "ttl": 600.0}),
        "health_check_cache_params": {
            "host": None,
            "port": None,
            "redis_kwargs": None,
            "namespace": None,
            "redis_version": None,
        },
    }


def ping_no_cache_text() -> str:
    """The 503 ``ProxyException`` message ``cache_ping`` raises when
    ``litellm.cache`` is None: a ``safe_dumps`` string, as ``backend._error_text``
    reads it back out of ``{"error": {"message": ...}}``
    (``openai_exception_handler`` renders ProxyException)."""
    return json.dumps(
        {
            "message": "Cache not initialized. litellm.cache is None",
            "litellm_cache_params": "{}",
            "health_check_cache_params": "{}",
        }
    )


def service_unhealthy_text(inner: str) -> str:
    """``cache_redis_info`` / ``cache_flushall`` wrap EVERY failure — their own
    inner HTTPExceptions included — in 503 ``Service Unhealthy ({e})``."""
    return f"Service Unhealthy ({inner})"


def cache_delete_failed_text(inner: str) -> str:
    """``cache_delete`` rewraps every failure as 500 ``Cache Delete Failed({e})``."""
    return f"Cache Delete Failed({inner})"


CLIENT = {
    "id": "42",
    "addr": "10.42.1.17:52318",
    "laddr": "10.42.4.7:6379",
    "name": "",
    "age": 3600,
    "db": "0",
    "sub": "0",
    "psub": "0",
    "ssub": "0",
    "multi": -1,
    "watch": 0,
    "qbuf": 32768,
    "cmd": "get",
}


def redis_info(*, no_clients: bool = False) -> dict[str, Any]:
    """``cache_redis_info`` (``proxy/caching_routes.py``): ``num_clients`` (-1
    when CLIENT LIST is restricted, common on managed Redis — the placeholder
    string then rides in ``clients``), the client rows, and ``redis-py``'s
    ``info()`` dict (``*_human`` spellings as redis-cli prints them, one
    ``dbN`` keyspace entry per logical database)."""
    info = {
        "redis_version": "7.2.4",
        "uptime_in_seconds": 432000,
        "uptime_in_days": 5,
        "connected_clients": 12,
        "blocked_clients": 0,
        "used_memory": 13107200,
        "used_memory_human": "12.50M",
        "used_memory_peak": 15728640,
        "used_memory_peak_human": "15.00M",
        "maxmemory": 0,
        "maxmemory_human": "0B",
        "maxmemory_policy": "noeviction",
        "keyspace_hits": 8213,
        "keyspace_misses": 1764,
        "evicted_keys": 0,
        "expired_keys": 37,
        "instantaneous_ops_per_sec": 14,
        "total_commands_processed": 99887,
        "db0": {"keys": 4321, "expires": 4300, "avg_ttl": 123456789},
        "db1": {"keys": 9, "expires": 0, "avg_ttl": 0},
    }
    if no_clients:
        return {
            "num_clients": -1,
            "clients": ["CLIENT LIST command not available on this Redis instance"],
            "info": info,
        }
    return {"num_clients": 3, "clients": [dict(CLIENT) for _ in range(3)], "info": info}


def _field(
    name: str,
    ftype: str,
    value: Any,
    description: str,
    default: Any = None,
    options: list[str] | None = None,
    ui: str | None = None,
    redis_type: str | None = None,
) -> dict[str, Any]:
    """One ``CacheSettingsField`` (``types/management_endpoints/
    cache_settings_endpoints.py``), with ``field_value`` set the way
    ``get_cache_settings`` sets it: the current value when the field is set."""
    return {
        "field_name": name,
        "field_type": ftype,
        "field_value": value,
        "field_description": description,
        "field_default": default,
        "options": options,
        "ui_field_name": ui or name.title(),
        "link": None,
        "redis_type": redis_type,
    }


REDIS_TYPE_DESCRIPTIONS = {
    "node": "Standard Redis node/single instance",
    "cluster": "Redis Cluster mode for high availability and horizontal scaling",
    "sentinel": "Redis Sentinel mode for high availability with automatic failover",
}


def settings_response(
    current: dict[str, Any] | None = None,
    *,
    redacted: bool = True,
) -> dict[str, Any]:
    """``CacheSettingsResponse`` from ``get_cache_settings``
    (``proxy/management_endpoints/cache_settings_endpoints.py``):
    ``current_values`` is the decrypted DB row overlaid with the REDIS_* env,
    with url precedence applied and ``redis_type`` derived (node | cluster
    from ``redis_startup_nodes`` | sentinel from ``sentinel_nodes``); every
    credential field (``password``, ``sentinel_password``, ``url`` — a redis
    URL can embed the password) is swapped for ``***REDACTED***``
    (``_redact_credentials``). ``fields`` carries the metadata of every
    ``CACHE_SETTINGS_FIELD``; a representative subset is reproduced here."""
    cv: dict[str, Any] = {
        "type": "redis",
        "host": "redis.litellm.svc",
        "port": "6379",
        "namespace": "litellm_proxy",
        "ttl": 300.0,
    }
    cv.update(current or {})
    if redacted:
        cv["password"] = "***REDACTED***"
        if cv.get("url") is not None:
            cv["url"] = "***REDACTED***"  # a redis URL can embed the password
    if cv.get("type") == "redis":
        if cv.get("redis_startup_nodes"):
            cv["redis_type"] = "cluster"
        elif cv.get("sentinel_nodes"):
            cv["redis_type"] = "sentinel"
        else:
            cv["redis_type"] = "node"
    fields = [
        _field(
            "redis_type",
            "String",
            cv.get("redis_type", "node"),
            "Type of Redis deployment",
            "node",
            ["node", "cluster", "sentinel"],
            "Redis Type",
        ),
        _field("host", "String", cv.get("host"), "Redis server hostname or IP address"),
        _field("port", "String", cv.get("port"), "Redis server port number", "6379"),
        _field("password", "String", cv.get("password"), "Redis server password"),
        _field("ttl", "Float", cv.get("ttl"), "Time-to-live for cached items in seconds"),
    ]
    return {
        "fields": fields,
        "current_values": cv,
        "redis_type_descriptions": dict(REDIS_TYPE_DESCRIPTIONS),
    }


def settings_updated(body: dict[str, Any]) -> dict[str, Any]:
    """``update_cache_settings`` (POST /cache/settings): the message, the
    status, and the saved settings as ``_redact_credentials`` masked them —
    the CLI re-masks client-side anyway."""
    cs = dict(body["cache_settings"])
    for k in ("password", "sentinel_password", "url"):
        if cs.get(k) is not None:
            cs[k] = "***REDACTED***"
    return {"message": "Cache settings updated successfully", "status": "success", "settings": cs}


def cache_test_ok() -> dict[str, Any]:
    """``CacheTestResponse`` from ``test_cache_connection`` after
    ``RedisCache.test_connection`` (``caching/redis_cache.py``) pinged a fresh
    client."""
    return {"status": "success", "message": "Redis connection test successful", "error": None}


def cache_test_failed(error: str) -> dict[str, Any]:
    """``test_cache_connection`` on a refused connection — still HTTP 200."""
    return {"status": "failed", "message": f"Redis connection failed: {error}", "error": error}


def cache_test_not_redis() -> dict[str, Any]:
    """``test_cache_connection`` for anything but a redis cache type."""
    return {
        "status": "failed",
        "message": "Only Redis cache type is currently supported for testing",
        "error": None,
    }


def cache_delete_ok() -> dict[str, Any]:
    """``cache_delete`` success."""
    return {"status": "success"}


def store_model_required_text() -> str:
    """``update_cache_settings`` without ``STORE_MODEL_IN_DB``: the 500 detail
    (``{"error": ...}`` inside ``detail``, as ``backend._error_text`` reads it)."""
    return "Set `'STORE_MODEL_IN_DB='True'` in your env to enable this feature."


def admin_denied_body(route: str) -> dict[str, Any]:
    """The live-captured 401 body every /cache/* route answers for a
    non-admin key (seen on the house v1.106.3 proxy, 2026-10-25):
    ``RouteChecks._raise_admin_only_route_exception``
    (``proxy/auth/route_checks.py``) wrapped by ``openai_exception_handler``."""
    return {
        "error": {
            "message": (
                "Authentication Error, Only proxy admin can be used to generate, delete, update "
                f"info for new keys/users/teams. Route={route}. Your role=unknown. Your user_id=*******"
            ),
            "type": "auth_error",
            "param": "None",
            "code": "401",
        }
    }
