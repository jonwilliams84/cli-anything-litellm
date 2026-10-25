"""Response shapes of LiteLLM v1.106.3's routing-resilience endpoints, for offline tests.

Paths are relative to ``litellm/`` in the v1.106.3 tree. Every factory names
the function whose response it reproduces.
"""

from __future__ import annotations

from typing import Any


def deployment(
    dep_id: str,
    name: str,
    *,
    db: bool = True,
    blocked: bool = False,
    base: str = "http://n1:8000/v1",
) -> dict[str, Any]:
    """One ``/v1/model/info`` entry. For a DB deployment
    ``ProxyConfig.get_model_info_with_id`` (``proxy/proxy_server.py``) sets
    ``model_info.id``, ``db_model: true`` and ``blocked`` (from
    ``LiteLLM_ProxyModelTable.blocked``); config deployments carry neither flag."""
    info: dict[str, Any] = {"id": dep_id, "mode": "chat"}
    if db:
        info.update(db_model=True, blocked=blocked)
    return {
        "model_name": name,
        "litellm_params": {"model": f"hosted_vllm/{name}", "api_base": base},
        "model_info": info,
    }


DEPLOYMENTS = [
    deployment("qwen-a", "qwen", base="http://n1:8000/v1"),
    deployment("qwen-b", "qwen", base="http://n2:8000/v1"),
    deployment("cfg-claude", "claude", db=False, base="https://api.anthropic.com"),
    deployment("gpt-1", "gpt-4o", base="https://api.openai.com/v1"),
    deployment("small-1", "small", blocked=True),
]


def router_settings(current: dict[str, Any], source: dict[str, str] | None = None) -> dict[str, Any]:
    """``RouterSettingsResponse`` from ``get_router_settings``
    (``proxy/management_endpoints/router_settings_endpoints.py``): ``fields``
    (``RouterSettingsField`` per setting), ``current_values`` (router attributes
    overlaid with the resolved ``router_settings``), descriptions, the routing
    group strategies, and ``source`` per current key (``FieldSource``:
    config | db | env | default | unset, ``config_resolvers/_descriptors.py``)."""
    values = {
        "routing_strategy": "simple-shuffle",
        "num_retries": 2,
        "fallbacks": [],
        "context_window_fallbacks": [],
        "content_policy_fallbacks": [],
        "routing_groups": [],
        **current,
    }
    src = {k: "default" for k in values}
    src.update(source or {})
    return {
        "fields": [
            {
                "field_name": "fallbacks",
                "field_type": "List",
                "field_value": values["fallbacks"],
                "field_description": "List of fallback model mappings",
                "field_default": [],
                "options": None,
                "ui_field_name": "Fallbacks",
                "link": None,
            }
        ],
        "current_values": values,
        "routing_strategy_descriptions": {"simple-shuffle": "Randomly picks a deployment from the list."},
        "routing_group_strategies": ["simple-shuffle", "least-busy", "priority"],
        "source": src,
    }


def fallback_get(model: str, chain: list[str], fallback_type: str = "general") -> dict[str, Any]:
    """``FallbackGetResponse`` (``types/management_endpoints/router_settings_endpoints.py``)
    from ``get_fallback`` (``proxy/management_endpoints/fallback_management_endpoints.py``)."""
    return {"model": model, "fallback_models": chain, "fallback_type": fallback_type}


def fallback_created(body: dict[str, Any], updated: bool = False) -> dict[str, Any]:
    """``FallbackResponse`` from ``create_fallback``."""
    return {
        **body,
        "message": f"Fallback configuration {'updated' if updated else 'created'} successfully",
    }


def fallback_deleted(model: str, fallback_type: str = "general") -> dict[str, Any]:
    """``FallbackDeleteResponse`` from ``delete_fallback``."""
    return {
        "model": model,
        "fallback_type": fallback_type,
        "message": "Fallback configuration deleted successfully",
    }


#: ``get_fallback`` / ``delete_fallback`` 404 detail, as ``backend._error_text`` reads it.
def no_fallback_text(model: str, fallback_type: str = "general") -> str:
    return f"No {fallback_type} fallbacks configured for model '{model}'"


def health_row(
    model_name: str,
    model_id: str | None,
    status: str = "healthy",
    *,
    at: str = "2026-10-25T08:00:00+00:00",
    error: str | None = None,
    by: str | None = "background_health_check",
) -> dict[str, Any]:
    """``convert_health_check_to_dict`` (``proxy/health_endpoints/_health_endpoints.py``):
    one ``LiteLLM_HealthCheckTable`` row; ``error_message`` is the first unhealthy
    endpoint's error cut to 500 characters (``_save_health_check_to_db``)."""
    unhealthy = status == "unhealthy"
    return {
        "health_check_id": f"hc-{model_id or model_name}-{at[11:16]}",
        "model_name": model_name,
        "model_id": model_id,
        "status": status,
        "healthy_count": 0 if unhealthy else 1,
        "unhealthy_count": 1 if unhealthy else 0,
        "error_message": error[:500] if error else None,
        "response_time_ms": 812.4567,
        "details": None,
        "checked_by": by,
        "checked_at": at,
        "created_at": at,
    }


UPSTREAM_ERROR = (
    "litellm.APIConnectionError: Hosted_vllmException - Connection refused calling "
    "http://n2:8000/v1/chat/completions?api_key=SECRET-IN-QUERY ; "
    "Incorrect API key provided: sk-proj-SECRETKEY123456. Bearer abcdefghSECRETTOKEN"
)

LATEST_ROWS = [
    health_row("qwen", "qwen-a"),
    health_row("qwen", "qwen-b", "unhealthy", error=UPSTREAM_ERROR),
    health_row("old", "deleted-1", "unhealthy", error="gone"),
    health_row("small", "small-1", "unhealthy", error="paused anyway"),
]


def latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """``latest_health_checks_endpoint``: keyed by model_id, else model_name."""
    return {
        "latest_health_checks": {(r["model_id"] or r["model_name"]): r for r in rows},
        "total_models": len(rows),
    }


def history(rows: list[dict[str, Any]], limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """``health_check_history_endpoint``: newest first, ``total_records`` = rows returned."""
    return {"health_checks": rows, "total_records": len(rows), "limit": limit, "offset": offset}


HISTORY_ROWS = [
    health_row("qwen", "qwen-b", "unhealthy", at="2026-10-25T08:10:00+00:00", error=UPSTREAM_ERROR),
    health_row("qwen", "qwen-b", "healthy", at="2026-10-24T22:00:00+00:00"),
    health_row("qwen", "qwen-a", "healthy", at="2026-10-24T21:59:00+00:00", by="admin-user"),
]


def blocked_row(dep_id: str, name: str, blocked: bool) -> dict[str, Any]:
    """The ``LiteLLM_ProxyModelTable`` row (``models/model.py``) that
    ``_set_model_blocked_status`` (``proxy/management_endpoints/model_management_endpoints.py``)
    returns for ``/model/block`` and ``/model/unblock`` — litellm_params included
    (stored encrypted), which is why the CLI never echoes it."""
    return {
        "model_id": dep_id,
        "model_name": name,
        "litellm_params": {"model": f"hosted_vllm/{name}", "api_key": "ENCRYPTED-SECRET-BLOB"},
        "model_info": {"id": dep_id, "mode": "chat"},
        "blocked": blocked,
        "created_at": "2026-10-20T09:00:00Z",
        "created_by": "admin",
        "updated_at": "2026-10-25T08:15:00Z",
        "updated_by": "admin",
    }


def completion_headers(dep_id: str, attempted: int) -> dict[str, str]:
    """Response headers of a proxied completion: ``get_custom_headers``
    (``proxy/common_request_processing.py``) sets ``x-litellm-model-id`` from the
    serving deployment; ``add_fallback_headers_to_response``
    (``router_utils/add_retry_fallback_headers.py``) adds the attempt count."""
    return {
        "x-litellm-call-id": "call-1",
        "x-litellm-model-id": dep_id,
        "x-litellm-attempted-fallbacks": str(attempted),
        "x-litellm-version": "1.106.3",
    }


COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "gpt-4o",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
}
