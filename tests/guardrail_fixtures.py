"""Response shapes of LiteLLM v1.106.3's guardrail endpoints, for offline tests.

Paths are relative to ``litellm/`` in the v1.106.3 tree. Every read the proxy
serves is already passed through ``get_masked_values``
(``litellm_core_utils/litellm_logging.py``), which keeps 2+2 characters of a
value under a sensitive key and puts four ``*`` between them — reproduced by
:func:`proxy_mask`.
"""

from __future__ import annotations

from typing import Any


def proxy_mask(v: str) -> str:
    """``get_masked_values(..., unmasked_length=4, number_of_asterisks=4)``."""
    return "*****" if len(v) <= 4 else v[:2] + "****" + v[-2:]


def guardrail(
    gid: str | None,
    name: str,
    integration: str = "presidio",
    mode: str | list[str] = "pre_call",
    *,
    location: str = "db",
    default_on: bool = False,
    **params: Any,
) -> dict[str, Any]:
    """``GuardrailInfoResponse`` (``types/guardrails.py``) as ``list_guardrails_v2`` and
    ``get_guardrail_info`` (``proxy/guardrails/guardrail_endpoints.py``) serialise it:
    ``litellm_params`` re-parsed into ``BaseLitellmParams`` (``extra="allow"`` keeps
    ``guardrail``/``mode``), masked, ``guardrail_definition_location`` db|config."""
    lp: dict[str, Any] = {"guardrail": integration, "mode": mode, "default_on": default_on}
    for k, v in params.items():
        lp[k] = proxy_mask(v) if isinstance(v, str) and any(w in k for w in ("key", "secret", "token")) else v
    return {
        "guardrail_id": gid,
        "guardrail_name": name,
        "litellm_params": lp,
        "guardrail_info": {"description": f"{name} guardrail"},
        "created_at": "2026-10-20T09:00:00Z" if location == "db" else None,
        "updated_at": "2026-10-20T09:00:00Z" if location == "db" else None,
        "guardrail_definition_location": location,
    }


GUARDS = [
    guardrail("cfg-bridge-1", "session-affinity-bridge", "custom_guardrail", "pre_call", location="config"),
    guardrail(
        "db-pii-1",
        "pii-mask",
        "presidio",
        ["pre_call", "post_call"],
        default_on=True,
        api_base="https://presidio.internal:3000/analyze?token=SECRET-IN-QUERY",
        api_key="sk-live-SECRET-KEY-123456",
    ),
    guardrail("db-inj-1", "prompt-injection", "lakera_v2", "pre_call"),
]

#: ``ListGuardrailsResponse`` (``list_guardrails_v2``).
V2_LIST = {"guardrails": GUARDS}

#: ``GET /guardrails/list`` (``list_guardrails``): config.yaml only, built by
#: ``_get_guardrails_list_response`` — no id-location field on config rows.
V1_LIST = {
    "guardrails": [
        {
            "guardrail_id": None,
            "guardrail_name": "session-affinity-bridge",
            "litellm_params": {"guardrail": "custom_guardrail", "mode": "pre_call", "default_on": False},
            "guardrail_info": None,
            "created_at": None,
            "updated_at": None,
            "guardrail_definition_location": "config",
        }
    ]
}


def created(body: dict[str, Any], gid: str = "db-new-1") -> dict[str, Any]:
    """``create_guardrail`` returns ``add_guardrail_to_db``'s dict: the request
    guardrail plus ``guardrail_id`` (``proxy/guardrails/guardrail_registry.py``),
    i.e. the UNMASKED request params come straight back."""
    return {**body["guardrail"], "guardrail_id": gid}


#: ``delete_guardrail_from_db``: ``{"message": "Guardrail <id> deleted successfully"}``.
def deleted(gid: str) -> dict[str, str]:
    return {"message": f"Guardrail {gid} deleted successfully"}


#: ``UsageOverviewResponse`` (``proxy/guardrails/usage_endpoints.py``,
#: ``guardrails_usage_overview``); camelCase as the model declares it.
OVERVIEW = {
    "rows": [
        {
            "id": "db-inj-1",
            "name": "prompt-injection",
            "type": "Guardrail",
            "provider": "lakera_v2",
            "requestsEvaluated": 40,
            "failRate": 20.0,
            "avgScore": None,
            "avgLatency": None,
            "status": "critical",
            "trend": "up",
            "usageUnits": {},
            "cost": None,
            "untrackedUsageUnits": {},
        },
        {
            "id": "db-pii-1",
            "name": "pii-mask",
            "type": "Guardrail",
            "provider": "presidio",
            "requestsEvaluated": 200,
            "failRate": 1.0,
            "avgScore": None,
            "avgLatency": None,
            "status": "healthy",
            "trend": "stable",
            "usageUnits": {"characters": 51200},
            "cost": 0.04,
            "untrackedUsageUnits": {},
        },
    ],
    "chart": [{"date": "2026-10-24", "passed": 230, "blocked": 10}],
    "totalRequests": 240,
    "totalBlocked": 10,
    "passRate": 95.8,
    "totalUsageUnits": {"characters": 51200},
    "totalCost": 0.04,
    "totalUntrackedUsageUnits": {},
}

#: ``_EMPTY_OVERVIEW`` — what a proxy without a DB returns.
EMPTY_OVERVIEW = {
    "rows": [],
    "chart": [],
    "totalRequests": 0,
    "totalBlocked": 0,
    "passRate": 100.0,
    "totalUsageUnits": {},
    "totalCost": None,
    "totalUntrackedUsageUnits": {},
}

#: ``UsageDetailResponse`` (``guardrails_usage_detail``).
DETAIL = {
    "guardrail_id": "db-pii-1",
    "guardrail_name": "pii-mask",
    "type": "Guardrail",
    "provider": "presidio",
    "requestsEvaluated": 200,
    "failRate": 1.0,
    "avgScore": None,
    "avgLatency": None,
    "status": "healthy",
    "trend": "stable",
    "description": "pii-mask guardrail",
    "time_series": [
        {"date": "2026-10-23", "passed": 99, "blocked": 1, "score": None},
        {"date": "2026-10-24", "passed": 99, "blocked": 1, "score": None},
    ],
    "usage_units": {"characters": 51200},
    "usage_units_daily": [{"date": "2026-10-24", "units": {"characters": 51200}, "cost": 0.04}],
    "usage_units_by_team": {"team-eng": {"characters": 51200}},
    "usage_units_by_key": {"88a3f0": {"characters": 51200}},
    "cost": 0.04,
    "cost_by_unit": {"characters": 0.04},
    "cost_by_team": {"team-eng": 0.04},
    "cost_by_key": {"88a3f0": 0.04},
    "untracked_usage_units": {},
    "untracked_usage_units_by_team": {},
    "untracked_usage_units_by_key": {},
}


def log_entry(rid: str, action: str, reason: str | None = None) -> dict[str, Any]:
    """``UsageLogEntry`` (``_usage_log_entry_from_row``)."""
    return {
        "id": rid,
        "timestamp": "2026-10-24T10:00:00+00:00",
        "action": action,
        "score": 0.93 if action == "blocked" else None,
        "latency_ms": 42.0,
        "model": "home-claude-fable-5-1",
        "input_snippet": "my card number is 4111 1111 1111 1111",
        "output_snippet": None,
        "reason": reason,
    }


#: ``UsageLogsResponse`` (``guardrails_usage_logs``).
LOGS = {
    "logs": [log_entry("req-2", "blocked", "Prompt injection detected"), log_entry("req-1", "passed")],
    "total": 2,
    "page": 1,
    "page_size": 50,
}

#: ``ListGuardrailSubmissionsResponse`` (``list_guardrail_submissions``), items from
#: ``_row_to_submission_item`` (params masked by ``get_masked_values``).
SUBMISSIONS = {
    "submissions": [
        {
            "guardrail_id": "sub-1",
            "guardrail_name": "team-eng-filter",
            "status": "pending_review",
            "team_id": "team-eng",
            "team_guardrail": True,
            "litellm_params": {
                "guardrail": "generic_guardrail_api",
                "mode": "pre_call",
                "api_base": "https://filter.eng.example/v1/check?apikey=SECRET-SUB",
                "api_key": proxy_mask("sk-team-SECRET-999"),
            },
            "guardrail_info": {
                "submitted_by_user_id": "alice",
                "submitted_by_email": "alice@corp.io",
                "team_guardrail": True,
            },
            "submitted_by_user_id": "alice",
            "submitted_by_email": "alice@corp.io",
            "submitted_at": "2026-10-24T08:00:00Z",
            "reviewed_at": None,
            "created_at": "2026-10-24T08:00:00Z",
            "updated_at": "2026-10-24T08:00:00Z",
        }
    ],
    "summary": {"total": 1, "pending_review": 1, "active": 0, "rejected": 0},
}

#: ``approve_guardrail_submission`` success body.
APPROVED = {"guardrail_id": "sub-1", "status": "active", "message": "Guardrail approved"}
