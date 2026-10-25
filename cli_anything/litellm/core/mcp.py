"""LiteLLM's MCP gateway (``/v1/mcp/*``): servers, health, tools, access, sessions.

A LiteLLM proxy can front MCP servers the way it fronts model deployments:
clients open one MCP endpoint on the proxy and see the tools of every server
their key may reach, prefixed ``<server>-<tool>``. Servers come from two
places, like models: ``mcp_servers:`` in config.yaml (``is_config: true``,
read-only through the API) and rows in the proxy's database, created with
``POST /v1/mcp/server``. Who may reach a server is the key's (or team's)
``object_permission``: explicit ``mcp_servers`` and/or ``mcp_access_groups``,
plus any server flagged ``allow_all_keys``.

The endpoints, as v1.106.3 has them (all under the ``/v1/mcp`` router prefix
in ``litellm/proxy/management_endpoints/mcp_management_endpoints.py``):

- ``GET /server`` (``fetch_all_mcp_servers``): a bare list of
  ``LiteLLM_MCPServerTable`` rows, credentials already stripped of secrets.
- ``GET /server/{id}`` (``fetch_mcp_server``): one row, after a live health
  check of that server.
- ``GET /server/health`` (``health_check_servers``): ``[{server_id, status}]``,
  status one of healthy / reachable / unhealthy / unknown / null.
- ``POST /server`` (``add_mcp_server``), ``PUT /server`` (``edit_mcp_server``,
  a partial update: omitted fields keep their stored value),
  ``DELETE /server/{id}`` (``remove_mcp_server``, 202 with an empty body).
- ``GET /tools`` (``get_mcp_tools``): ``{"tools": [...]}`` for the CALLING key.
- ``GET /access_groups`` (``get_mcp_access_groups``): ``{"access_groups": [...]}``.
- ``GET /sessions`` / ``DELETE /sessions``: live stateful gateway sessions on
  the ONE worker process that answered (``worker_pid``), not the fleet.

Secrets: an admin read of a server can still carry an upstream key in places
the proxy does not treat as credentials: the URL (the proxy's own comment says
many integrations embed the key in the path), ``static_headers`` values,
``env`` / ``env_vars`` values and stdio ``args``. :func:`redact_server` cuts
all of them down before anything is printed.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote, urlsplit

#: ``MCP_TOOL_PREFIX_SEPARATOR`` (``_experimental/mcp_server/utils.py``): the
#: proxy refuses server names and aliases containing it, because tool names are
#: ``<server><sep><tool>``.
TOOL_SEPARATOR = "-"

#: ``SpecialMCPServerNames.no_mcp_servers`` (``proxy/_types.py``): a key whose
#: ``mcp_servers`` holds this sentinel reaches no MCP server at all.
NO_SERVERS = "no-mcp-servers"

#: ``SpecialMCPServerName`` values the proxy refuses as a server id.
RESERVED_IDS = ("all-team-mcpservers", "all-proxy-mcpservers")

#: ``MCPAuth`` (``litellm/types/mcp.py``).
AUTH_TYPES = (
    "none",
    "api_key",
    "bearer_token",
    "basic",
    "authorization",
    "oauth2",
    "aws_sigv4",
    "token",
    "oauth2_token_exchange",
    "oauth2_id_jag",
    "true_passthrough",
    "oauth_delegate",
)

#: Transports the CLI will create. ``stdio`` is deliberately absent: it spawns
#: a process on the proxy host, and belongs in config.yaml where it is reviewed.
TRANSPORTS = ("http", "sse")

#: ``MCP_GATEWAY_SESSION_ID_PREFIX_LENGTH`` (``litellm/constants.py``): the
#: shortest session id prefix ``DELETE /v1/mcp/sessions`` accepts.
SESSION_PREFIX_MIN = 8

MASK = "****"


def as_rows(res: Any, key: str | None = None) -> list[dict[str, Any]]:
    """A list out of whatever came back: bare list, or ``{key: [...]}``."""
    if isinstance(res, list):
        return [r for r in res if isinstance(r, dict)]
    if isinstance(res, dict):
        for k in (key, "data", "servers", "tools"):
            if k and isinstance(res.get(k), list):
                return [r for r in res[k] if isinstance(r, dict)]
    return []


def server_path(server_id: str) -> str:
    """``/v1/mcp/server/{id}``, with the id escaped (a pinned id may hold any character)."""
    return f"/v1/mcp/server/{quote(str(server_id), safe='')}"


def server_label(s: dict[str, Any]) -> str:
    """The name clients see in tool prefixes: alias, else server_name, else id."""
    return s.get("alias") or s.get("server_name") or s.get("server_id") or "?"


def url_origin(url: Any) -> str | None:
    """``scheme://host[:port]`` plus ``/…`` when a path or query was cut off.

    Userinfo, path and query are dropped: any of them can carry an upstream
    API key, and the origin is what an operator needs to recognise a backend.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return MASK
    if not parts.scheme or not host:
        return MASK
    origin = f"{parts.scheme}://{host}" + (f":{port}" if port else "")
    rest = (parts.path or "").strip("/") or parts.query
    return origin + ("/…" if rest else "")


def _masked_map(m: Any) -> dict[str, str] | None:
    if not isinstance(m, dict) or not m:
        return None
    return dict.fromkeys(sorted(str(k) for k in m), MASK)


def redact_server(s: dict[str, Any]) -> dict[str, Any]:
    """A copy of a server row that is safe to print.

    Keeps names, never values: ``credentials`` becomes the sorted list of the
    credential fields that are set, header/env maps keep their keys with
    masked values, ``env_vars`` keep name and scope with ``value_set``.
    """
    out = dict(s)
    out["url"] = url_origin(s.get("url"))
    if s.get("spec_path"):
        out["spec_path"] = url_origin(s["spec_path"]) if "://" in str(s["spec_path"]) else MASK
    creds = s.get("credentials")
    out["credentials"] = (
        sorted(k for k, v in creds.items() if v not in (None, "", [])) if isinstance(creds, dict) else None
    )
    out["static_headers"] = _masked_map(s.get("static_headers"))
    out["env"] = _masked_map(s.get("env"))
    env_vars = s.get("env_vars")
    out["env_vars"] = (
        [
            {"name": v.get("name"), "scope": v.get("scope"), "value_set": bool(v.get("value"))}
            for v in env_vars
            if isinstance(v, dict)
        ]
        if isinstance(env_vars, list)
        else None
    )
    if s.get("args"):
        out["args"] = [MASK] * len(s["args"])
    return out


def normalize(s: dict[str, Any], status: str | None = None) -> dict[str, Any]:
    """One row per server, as `mcp list` shows it (table and --json)."""
    teams = s.get("teams") or []
    return {
        "server_id": s.get("server_id"),
        "name": server_label(s),
        "server_name": s.get("server_name"),
        "source": "config" if s.get("is_config") else "db",
        "transport": s.get("transport"),
        "auth_type": s.get("auth_type"),
        "url": url_origin(s.get("url")),
        "access_groups": list(s.get("mcp_access_groups") or []),
        "allowed_tools": list(s.get("allowed_tools") or []),
        "allow_all_keys": bool(s.get("allow_all_keys")),
        "teams": [t.get("team_alias") or t.get("team_id") for t in teams if isinstance(t, dict)],
        "approval_status": s.get("approval_status"),
        "status": status if status is not None else s.get("status"),
        "description": s.get("description"),
    }


def resolve_server(servers: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """Match ``ref`` against live servers: exact server_id, then a UNIQUE alias/server_name.

    Returns ``{by, server}`` or None. Raises ValueError when a name matches
    more than one server (the proxy keeps names unique case-insensitively, but
    a config server and a DB server can still collide on display name).
    """
    if not ref:
        return None
    for s in servers:
        if s.get("server_id") == ref:
            return {"by": "server_id", "server": s}
    hits = [s for s in servers if ref in (s.get("alias"), s.get("server_name"))]
    if not hits:
        low = ref.lower()
        hits = [
            s
            for s in servers
            if low in ((s.get("alias") or "").lower(), (s.get("server_name") or "").lower())
        ]
    if len(hits) > 1:
        raise ValueError(f"{ref!r} names {len(hits)} MCP servers — use the server_id (`mcp list`)")
    return {"by": "name", "server": hits[0]} if hits else None


def check_name(value: str | None, flag: str) -> None:
    """The proxy 400s on a name containing the tool separator; say so first."""
    if value and TOOL_SEPARATOR in value:
        raise ValueError(
            f"{flag} {value!r} contains {TOOL_SEPARATOR!r}, which LiteLLM reserves to join "
            f"server and tool names (use '_' instead)"
        )


def split_csv(raw: str | None) -> list[str] | None:
    """``a, b,,c`` → ``["a", "b", "c"]``; None stays None (flag not given)."""
    if raw is None:
        return None
    return [p.strip() for p in raw.split(",") if p.strip()]


def secret_from_env(var: str | None, env: dict[str, str] | None = None) -> str | None:
    """The upstream credential, read from the CLI's own environment.

    MCP credentials have no ``os.environ/`` indirection on the proxy (unlike
    model ``api_key``), so the value has to be sent; taking it from an env
    var keeps it off the command line and out of shell history.
    """
    if not var:
        return None
    val = (env if env is not None else os.environ).get(var)
    if not val:
        raise ValueError(f"--auth-value-env {var}: that environment variable is unset or empty")
    return val


def new_server(
    name: str,
    *,
    url: str | None,
    transport: str = "http",
    server_id: str | None = None,
    alias: str | None = None,
    description: str | None = None,
    auth_type: str | None = None,
    auth_value: str | None = None,
    access_groups: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    allow_all_keys: bool = False,
) -> dict[str, Any]:
    """Build the ``POST /v1/mcp/server`` body (``NewMCPServerRequest``). Raises ValueError.

    ``transport`` is always sent: the request model defaults it to ``sse``,
    which is rarely what a modern streamable-HTTP server wants.
    """
    if not name:
        raise ValueError("--name is required")
    check_name(name, "--name")
    check_name(alias, "--alias")
    if transport not in TRANSPORTS:
        raise ValueError(f"--transport must be one of {', '.join(TRANSPORTS)}")
    if not url:
        raise ValueError(f"--url is required for {transport} transport")
    if server_id in RESERVED_IDS:
        raise ValueError(f"server id {server_id!r} is reserved by LiteLLM")
    if auth_type is not None and auth_type not in AUTH_TYPES:
        raise ValueError(f"--auth-type must be one of {', '.join(AUTH_TYPES)}")
    if auth_value and not auth_type:
        raise ValueError("--auth-value-env needs --auth-type (e.g. bearer_token)")
    body: dict[str, Any] = {
        k: v
        for k, v in {
            "server_id": server_id,
            "server_name": name,
            "alias": alias,
            "description": description,
            "transport": transport,
            "url": url,
            "auth_type": auth_type,
            "mcp_access_groups": access_groups or None,
            "allowed_tools": allowed_tools or None,
            "allow_all_keys": True if allow_all_keys else None,
        }.items()
        if v is not None
    }
    if auth_value:
        body["credentials"] = {"auth_value": auth_value}
    return body


def update_server(
    server_id: str,
    *,
    alias: str | None = None,
    description: str | None = None,
    url: str | None = None,
    transport: str | None = None,
    auth_type: str | None = None,
    auth_value: str | None = None,
    access_groups: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    allow_all_keys: bool | None = None,
) -> dict[str, Any]:
    """Build the ``PUT /v1/mcp/server`` body (``UpdateMCPServerRequest``). Raises ValueError.

    The proxy writes only the fields present (``exclude_unset``), so unset
    flags are left out rather than sent as null — a null would CLEAR the field.
    A list flag given as an empty string clears that list on purpose.
    """
    check_name(alias, "--alias")
    if transport is not None and transport not in TRANSPORTS:
        raise ValueError(f"--transport must be one of {', '.join(TRANSPORTS)}")
    if auth_type is not None and auth_type not in AUTH_TYPES:
        raise ValueError(f"--auth-type must be one of {', '.join(AUTH_TYPES)}")
    changes: dict[str, Any] = {
        k: v
        for k, v in {
            "alias": alias,
            "description": description,
            "url": url,
            "transport": transport,
            "auth_type": auth_type,
            "mcp_access_groups": access_groups,
            "allowed_tools": allowed_tools,
            "allow_all_keys": allow_all_keys,
        }.items()
        if v is not None
    }
    if auth_value:
        changes["credentials"] = {"auth_value": auth_value}
    if not changes:
        raise ValueError("nothing to update")
    return {"server_id": server_id, **changes}


def masked_body(body: dict[str, Any], env_var: str | None = None) -> dict[str, Any]:
    """The request body as `--dry-run` prints it: the credential never appears."""
    if "credentials" not in body:
        return body
    shown = dict(body)
    shown["credentials"] = {
        k: (f"{MASK} (from ${env_var})" if env_var else MASK) for k in body["credentials"]
    }
    return shown


def tool_server(tool_name: str, labels: list[str]) -> str | None:
    """Which server a prefixed tool name belongs to (longest matching label wins)."""
    best = None
    for lab in labels:
        if tool_name.startswith(lab + TOOL_SEPARATOR) and (best is None or len(lab) > len(best)):
            best = lab
    if best is None and TOOL_SEPARATOR in tool_name:
        return tool_name.split(TOOL_SEPARATOR, 1)[0]
    return best


def normalize_tool(t: dict[str, Any], labels: list[str]) -> dict[str, Any]:
    """One row per tool. ``annotations`` are the server's own hints, not enforced."""
    ann = t.get("annotations") or {}
    name = t.get("name") or ""
    server = tool_server(name, labels)
    desc = (t.get("description") or "").strip().splitlines()
    return {
        "name": name,
        "server": server,
        "tool": name[len(server) + len(TOOL_SEPARATOR) :] if server and name.startswith(server) else name,
        "title": t.get("title"),
        "read_only": ann.get("readOnlyHint"),
        "destructive": ann.get("destructiveHint"),
        "description": desc[0][:100] if desc else None,
    }


#: ``LiteLLM_ObjectPermissionBase`` (``proxy/_types.py``): the grant fields a
#: key/team request accepts. ``/key/info`` returns the stored row instead —
#: these plus ``object_permission_id`` and null relation fields.
PERMISSION_FIELDS = (
    "mcp_servers",
    "mcp_access_groups",
    "mcp_tool_permissions",
    "mcp_toolsets",
    "blocked_tools",
    "vector_stores",
    "agents",
    "agent_access_groups",
    "models",
    "search_tools",
    "mcp_tool_search_enabled",
    "skills",
)


def permission_body(stored: Any) -> dict[str, Any] | None:
    """The request-shaped grant out of a stored ``object_permission`` row.

    Drops the row id (it belongs to the OLD key — reusing it would make two
    keys share, and later co-edit, one permission row) and empty fields.
    """
    if not isinstance(stored, dict):
        return None
    out = {k: stored[k] for k in PERMISSION_FIELDS if stored.get(k) not in (None, [], {})}
    return out or None


def key_mcp(k: dict[str, Any]) -> dict[str, Any]:
    """A key's MCP grant, from the ``object_permission`` /key/list attaches."""
    op = k.get("object_permission") or {}
    return {
        "access_groups": list(op.get("mcp_access_groups") or []),
        "servers": list(op.get("mcp_servers") or []),
        "tool_permissions": op.get("mcp_tool_permissions") or None,
    }


def effective_servers(grant: dict[str, Any], servers: list[dict[str, Any]]) -> list[str] | None:
    """Server labels a key reaches through its OWN grant (groups ∪ servers ∪ allow_all_keys).

    None when the key carries no MCP grant of its own: what it reaches then
    depends on its team and role (an admin key reaches everything), which
    this view does not try to reproduce.
    """
    if NO_SERVERS in grant["servers"]:
        return []
    if not grant["access_groups"] and not grant["servers"]:
        return None
    groups = set(grant["access_groups"])
    named = set(grant["servers"])
    out = []
    for s in servers:
        ids = {s.get("server_id"), s.get("alias"), s.get("server_name")} - {None}
        if ids & named or groups & set(s.get("mcp_access_groups") or []) or s.get("allow_all_keys"):
            out.append(server_label(s))
    return sorted(out)


def grant_permission(
    servers: list[dict[str, Any]],
    known_groups: list[str],
    groups_raw: str | None,
    servers_raw: str | None,
) -> dict[str, Any] | None:
    """The ``object_permission`` for `keys generate|update --mcp-groups/--mcp-servers`.

    Server refs are resolved to server_ids (an unknown or ambiguous ref is a
    ValueError, before the proxy is called); ``none`` is the explicit opt-out
    sentinel. Groups must exist on the proxy: a typo would otherwise grant
    nothing, silently. Returns None when neither flag was given. The proxy
    merges ``object_permission`` per field on update, so sending only
    ``mcp_access_groups`` leaves the key's other grants alone.
    """
    if groups_raw is None and servers_raw is None:
        return None
    perm: dict[str, Any] = {}
    if groups_raw is not None:
        groups = split_csv(groups_raw) or []
        unknown = sorted(set(groups) - set(known_groups))
        if unknown:
            raise ValueError(
                f"unknown MCP access group(s) {', '.join(unknown)} — the proxy has: "
                f"{', '.join(known_groups) or '(none)'} (`mcp groups`)"
            )
        perm["mcp_access_groups"] = groups
    if servers_raw is not None:
        refs = split_csv(servers_raw) or []
        if [r.lower() for r in refs] == ["none"]:
            perm["mcp_servers"] = [NO_SERVERS]
        else:
            ids = []
            for ref in refs:
                hit = resolve_server(servers, ref)
                if not hit:
                    raise ValueError(f"no MCP server matches {ref!r} — run `mcp list`")
                ids.append(hit["server"]["server_id"])
            perm["mcp_servers"] = ids
    return perm


def keys_granting(keys: list[dict[str, Any]], server: dict[str, Any]) -> list[dict[str, Any]]:
    """Keys whose own grant names ``server`` explicitly (by id, alias or name)."""
    ids = {server.get("server_id"), server.get("alias"), server.get("server_name")} - {None}
    return [k for k in keys if ids & set(key_mcp(k)["servers"])]
