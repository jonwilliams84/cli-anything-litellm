"""Response shapes of LiteLLM v1.106.3's MCP gateway endpoints, for offline tests.

Every factory names the source it reproduces (paths relative to
``litellm/`` in the v1.106.3 tree). The server row was also checked
field-for-field against ``GET /v1/mcp/server`` on a live v1.106.3 proxy.
"""

from __future__ import annotations

import hashlib
from typing import Any


def mcp_server(
    server_id: str,
    name: str,
    *,
    alias: str | None = None,
    is_config: bool = False,
    url: str | None = None,
    groups: tuple[str, ...] = ("devtools",),
    auth_type: str | None = None,
    allow_all_keys: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """One ``LiteLLM_MCPServerTable`` row (``models/mcp_server.py``), as
    ``fetch_all_mcp_servers`` returns it after ``_redact_mcp_credentials``
    (``proxy/management_endpoints/mcp_management_endpoints.py``): secret
    credential keys gone, ``mcp_info.is_public`` stamped. Config servers come
    back with ``url: null`` and ``created_*: null`` (``build_mcp_server_table``
    in ``proxy/_experimental/mcp_server/mcp_server_manager.py``)."""
    row: dict[str, Any] = {
        "server_id": server_id,
        "is_config": is_config,
        "server_name": name,
        "alias": alias,
        "description": f"{name} tools",
        "url": url,
        "spec_path": None,
        "transport": "http",
        "auth_type": auth_type,
        "credentials": None,
        "instructions": None,
        "created_at": None if is_config else "2026-10-20T09:00:00Z",
        "created_by": None if is_config else "default_user_id",
        "updated_at": None if is_config else "2026-10-20T09:00:00Z",
        "updated_by": None if is_config else "default_user_id",
        "teams": [],
        "mcp_access_groups": list(groups),
        "allowed_tools": [],
        "tool_name_to_display_name": None,
        "tool_name_to_description": None,
        "pinned_tools": None,
        "extra_headers": [],
        "mcp_info": {
            "server_name": name,
            "description": f"{name} tools",
            "is_public": False,
            "is_public_explicit": False,
        },
        "static_headers": None,
        "env_vars": None,
        "status": None,
        "last_health_check": None,
        "health_check_error": None,
        "command": None,
        "args": [],
        "env": {},
        "issuer": None,
        "authorization_url": None,
        "token_url": None,
        "registration_url": None,
        "oauth2_flow": None,
        "token_exchange_endpoint": None,
        "audience": None,
        "subject_token_type": None,
        "token_exchange_profile": None,
        "allow_all_keys": allow_all_keys,
        "available_on_public_internet": True,
        "delegate_auth_to_upstream": False,
        "oauth_passthrough": False,
        "dcr_bridge": None,
        "per_server_oauth_discovery": False,
        "is_byok": False,
        "byok_description": [],
        "byok_api_key_help_url": None,
        "has_user_credential": None,
        "connected_app_reachable": None,
        "source_url": None,
        "timeout": None,
        "max_concurrent_requests": None,
        "rpm": None,
        "approval_status": "active",
        "submitted_by": None,
        "submitted_at": None,
        "reviewed_at": None,
        "review_notes": None,
    }
    row.update(extra)
    return row


#: Three config servers as a live v1.106.3 gateway reports them (ids included),
#: plus a DB server whose URL query, header and env var carry fake secrets.
SERVERS = [
    mcp_server("25c28049fb4adbad3811686e95a66f88", "context7", is_config=True),
    mcp_server("222c4d8eceea436fb9b4e52a56eae967", "grafana", is_config=True, auth_type="bearer_token"),
    mcp_server("8af4156217af9d9a59a5c273253b802c", "kubernetes", is_config=True),
    mcp_server(
        "db-ha-0001",
        "home_assistant",
        alias="ha",
        url="https://ha.example:8123/api/mcp?token=SECRET-IN-QUERY",
        groups=("home",),
        auth_type="bearer_token",
        static_headers={"X-Api-Key": "SECRET-HEADER"},
        env_vars=[{"name": "HA_TOKEN", "value": "SECRET-ENV", "scope": "global", "description": None}],
    ),
]


def health(statuses: dict[str, str | None]) -> list[dict[str, Any]]:
    """``GET /v1/mcp/server/health`` (``health_check_servers``): a bare list."""
    return [{"server_id": sid, "status": st} for sid, st in statuses.items()]


def tool(name: str, read_only: bool | None = True, description: str = "Does a thing.") -> dict[str, Any]:
    """One entry of ``GET /v1/mcp/tools`` (``get_mcp_tools``): an MCP ``Tool``
    ``model_dump(by_alias=True)``, names prefixed ``<server>-<tool>``
    (``add_server_prefix_to_name`` in ``proxy/_experimental/mcp_server/utils.py``)."""
    return {
        "name": name,
        "title": None,
        "description": description,
        "inputSchema": {"type": "object", "properties": {}},
        "execution": None,
        "outputSchema": None,
        "icons": None,
        "annotations": {
            "title": None,
            "readOnlyHint": read_only,
            "destructiveHint": None if read_only is None else not read_only,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        "_meta": None,
    }


TOOLS = {
    "tools": [
        tool("context7-resolve-library-id"),
        tool("context7-query-docs"),
        tool("grafana-query_prometheus"),
        tool("kubernetes-pods_list"),
        tool("ha-light_turn_on", read_only=False, description="Turn a light on.\nSecond line."),
    ]
}

#: ``GET /v1/mcp/access_groups`` (``get_mcp_access_groups``): sorted, config ∪ DB.
GROUPS = {"access_groups": ["devtools", "home"]}


def session(prefix: str, client: str, user: str, alias: str) -> dict[str, Any]:
    """``MCPGatewaySession`` (``types/mcp.py``)."""
    return {
        "session_id_prefix": prefix,
        "client_name": client,
        "client_version": "1.0.0",
        "user_id": user,
        "user_email": None,
        "key_alias": alias,
        "team_id": None,
        "team_alias": None,
        "client_ip": "10.42.0.7",
        "idle_seconds": 12.5,
        "in_flight_requests": 0,
    }


SESSIONS = {
    # MCPGatewaySessionsResponse (types/mcp.py), from get_mcp_gateway_sessions_report
    # (proxy/_experimental/mcp_server/server.py)
    "worker_pid": 41,
    "total_sessions": 2,
    "by_client": [{"label": "claude-code", "count": 1}, {"label": "opencode", "count": 1}],
    "by_user": [{"label": "jon", "count": 2}],
    "sessions": [
        session("a1b2c3d4", "claude-code", "jon", "jon-claude-code"),
        session("e5f6a7b8", "opencode", "jon", "cloudroom"),
    ],
}


def terminated(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """``MCPGatewaySessionsTerminateResponse`` (``types/mcp.py``)."""
    return {"worker_pid": 41, "terminated_sessions": len(sessions), "sessions": sessions}


def permission(groups=(), servers=(), **extra: Any) -> dict[str, Any]:
    """A stored ``LiteLLM_ObjectPermissionTable`` row (``proxy/schema.prisma``)
    as ``attach_object_permission_to_dict`` (``proxy/management_helpers/
    object_permission_utils.py``) attaches it to ``/key/list`` and ``/key/info``
    rows: every column plus null relation fields. Matches a live v1.106.3
    ``/key/info``."""
    row: dict[str, Any] = {
        "object_permission_id": "12b33b4c-0bf0-46c6-af9b-83ff6e621f8f",
        "mcp_servers": list(servers),
        "mcp_access_groups": list(groups),
        "mcp_tool_permissions": None,
        "vector_stores": [],
        "agents": [],
        "agent_access_groups": [],
        "models": [],
        "blocked_tools": [],
        "mcp_toolsets": [],
        "search_tools": [],
        "mcp_tool_search_enabled": None,
        "skills": [],
        "teams": None,
        "projects": None,
        "verification_tokens": None,
        "organizations": None,
        "users": None,
        "end_users": None,
        "agents_table": None,
    }
    row.update(extra)
    return row


def key(alias: str, perm: dict[str, Any] | None = None, team: str | None = None) -> dict[str, Any]:
    """A ``/key/list?return_full_object=true`` row (``_list_key_helper`` in
    ``proxy/management_endpoints/key_management_endpoints.py``), trimmed to the
    fields the CLI reads."""
    return {
        "token": hashlib.sha256(alias.encode()).hexdigest(),
        "key_alias": alias,
        "key_name": "sk-...xxxx",
        "team_id": team,
        "user_id": None,
        "spend": 0.0,
        "max_budget": None,
        "metadata": {},
        "object_permission_id": perm and perm["object_permission_id"],
        "object_permission": perm,
    }
