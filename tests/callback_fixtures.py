"""Response shapes of LiteLLM v1.106.3's logging-callback endpoints, for
offline tests.

Paths are relative to ``litellm/`` in the v1.106.3 tree. Every factory names
the function whose response it reproduces. All of these were read from that
source and none could be field-checked live (the key this CLI runs as on the
house estate is not a proxy admin, and ``POST /config/update`` /
``/config/callback/delete`` are admin-only with the DB required — the error
shapes below are what the source raises before the write happens).
"""

from __future__ import annotations

from typing import Any

LANGFUSE_PUBLIC = "pk-lf-00000000"
LANGFUSE_SECRET = "sk-lf-00000000-0000-0000-0000-000000000000"
SLACK_WEBHOOK = "https://hooks.slack.com/services/T0000000/B0000000/abcdefghijklmnop"
LAGO_KEY = "lago-api-key-0000"


def by_type_response() -> dict[str, list[str]]:
    """``callback_management_endpoints.list_callbacks`` — response_model
    ``CallbacksByType`` (``litellm/types/utils.py``), from
    ``logging_callback_manager.get_callbacks_by_type()``: a canonical-name list
    per event type; the internal loggers (spend tracking, alerting) are hidden
    here."""
    return {"success": ["langfuse", "otel"], "failure": [], "success_and_failure": []}


def by_type_scalar_response() -> dict[str, Any]:
    """An older proxy's ``CallbacksByType`` with scalar members — tolerated."""
    return {"success": "langfuse", "failure": None}


def callbacks_config_response(masked: bool = False) -> dict[str, Any]:
    """``proxy_server.get_config`` (``GET /get/config/callbacks``, Admin-UI
    reader). Callback rows: the stored ones (``process_callback`` over
    ``litellm_settings.success_callback/callbacks``, here langfuse as success
    and lago as success_and_failure) plus the runtime-only ones
    (``_hidden_runtime_callback_names`` filtered: an otel instance wired by env
    comes back ``read_only``). An admin key reads real variable values (stored
    plaintext, or decrypted from the encrypted ``environment_variables``
    section); a non-admin key gets the proxy's ``REDACTED`` on the
    credential-named ones (``_apply_callback_role_gate``).
    """
    public = "pk-lf-public" if masked else LANGFUSE_PUBLIC
    secret = "REDACTED" if masked else LANGFUSE_SECRET
    lago = "REDACTED" if masked else LAGO_KEY
    webhook = "REDACTED" if masked else SLACK_WEBHOOK
    return {
        "status": "success",
        "callbacks": [
            {
                "name": "langfuse",
                "variables": {
                    "LANGFUSE_PUBLIC_KEY": public,
                    "LANGFUSE_SECRET_KEY": secret,
                    "LANGFUSE_HOST": "https://cloud.langfuse.com",
                },
                "type": "success",
            },
            {
                "name": "lago",
                "variables": {
                    "LAGO_API_BASE": "https://lago.example",
                    "LAGO_API_KEY": lago,
                    "LAGO_API_CHARGE_BY": "team",
                },
                "type": "success_and_failure",
            },
            {"name": "otel", "variables": {"OTEL_ENDPOINT": "http://otel.example", "OTEL_HEADERS": secret}, "type": "success", "read_only": True},
        ],
        "active_alerting_destinations": ("slack",),
        "alerts": [
            {
                "name": "slack",
                "variables": {
                    "SLACK_WEBHOOK_URL": webhook,
                    "SLACK_DND_MODE": "False",
                },
                "active_alerts": ["catch_all", "budget_alerts"],
                "alerts_to_webhook": {},
            },
            {"name": "email", "variables": {"SMTP_HOST": "smtp.example"}},
        ],
        "router_settings": {"routing_strategy": "simple-shuffle"},
        "available_callbacks": {
            "langfuse": {
                "litellm_callback_name": "langfuse",
                "litellm_callback_params": ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"],
                "ui_callback_name": "Langfuse",
            },
            "datadog": {
                "litellm_callback_name": "datadog",
                "litellm_callback_params": ["DD_API_KEY", "DD_SITE"],
                "ui_callback_name": "Datadog",
            },
        },
    }


def update_ok() -> dict[str, str]:
    """``proxy_server.update_config`` success reply."""
    return {"message": "Config updated successfully"}


def delete_ok(callback: str = "langfuse", remaining: tuple[str, ...] = ("lago",)) -> dict[str, Any]:
    """``proxy_server.delete_callback`` (``POST /config/callback/delete``)
    success reply: the proxy restarts workers to apply it."""
    return {
        "message": f"Successfully deleted callback: {callback}",
        "removed_callback": callback,
        "remaining_callbacks": list(remaining),
        "deleted_at": "2026-11-09T12:00:00.000000",
    }


def delete_not_found(callback: str = "galileo") -> dict[str, Any]:
    """``delete_callback``'s 404: the lower-cased name was not in the stored
    ``success_callback`` list — which is also what you get for a
    failure/success_and_failure row the endpoint cannot reach."""
    return {"detail": {"error": f"Callback '{callback}' not found in active configuration"}}


def config_owned_error(key: str = "success_callback", section: str = "litellm_settings") -> dict[str, Any]:
    """``ProxyConfig.reject_config_owned_writes`` (a 400 on ``POST
    /config/update``): the key is set in config.yaml, so the DB row it would
    write is shadowed at every restart and the change is refused."""
    return {
        "detail": {
            "error": (
                f"{section} key '{key}' is set in the config file and cannot be changed here."
                " edit config.yaml to change it, or remove it from the file to let the database own it"
            ),
            "keys": [key],
            "section": section,
            "stored_database_values_ignored": [],
            "resolution": f"edit config.yaml to change it, or remove it from the file to let the database own {key}",
        }
    }
