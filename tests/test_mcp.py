"""MCP gateway coverage (`mcp ...`, `keys --mcp-*`, v0.10.0): core units, CLI E2E, workflows.

Fixtures (tests/mcp_fixtures.py) reproduce LiteLLM v1.106.3 response shapes;
each factory cites the source function it mirrors.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from cli_anything.litellm.core import mcp as mcpc

from mcp_fixtures import GROUPS, SERVERS, SESSIONS, TOOLS, health, key, mcp_server, permission, terminated
from test_harness import run


# ── core/mcp.py ───────────────────────────────────────────────────────────


def test_mcp_as_rows_tolerates_list_and_envelopes():
    assert mcpc.as_rows(SERVERS) == SERVERS
    assert mcpc.as_rows(TOOLS, "tools") == TOOLS["tools"]
    assert mcpc.as_rows({"data": [{"a": 1}, "junk"]}) == [{"a": 1}]
    assert mcpc.as_rows(None) == [] and mcpc.as_rows("text") == []


def test_mcp_server_label_prefers_alias_then_name_then_id():
    assert mcpc.server_label(SERVERS[3]) == "ha"
    assert mcpc.server_label(SERVERS[0]) == "context7"
    assert mcpc.server_label({"server_id": "x"}) == "x"


@pytest.mark.parametrize(
    "url,want",
    [
        ("https://ha.example:8123/api/mcp?token=S", "https://ha.example:8123/…"),
        ("https://user:pw@mcp.example/", "https://mcp.example"),
        ("http://10.0.0.5:9000", "http://10.0.0.5:9000"),
        ("https://mcp.example/?key=S", "https://mcp.example/…"),
        ("not a url", "****"),
        (None, None),
        ("", None),
    ],
)
def test_mcp_url_origin_drops_userinfo_path_and_query(url, want):
    assert mcpc.url_origin(url) == want


def test_mcp_redact_server_never_carries_a_secret_value():
    out = mcpc.redact_server(
        {
            **SERVERS[3],
            "credentials": {"auth_value": "SECRET-CRED", "scopes": []},
            "args": ["--token", "SECRET-ARG"],
        }
    )
    dumped = json.dumps(out)
    assert "SECRET" not in dumped
    assert out["credentials"] == ["auth_value"]
    assert out["static_headers"] == {"X-Api-Key": "****"}
    assert out["env_vars"] == [{"name": "HA_TOKEN", "scope": "global", "value_set": True}]
    assert out["args"] == ["****", "****"]
    assert out["server_name"] == "home_assistant"  # non-secret fields pass through


def test_mcp_redact_server_handles_config_row_with_nulls():
    out = mcpc.redact_server(SERVERS[0])
    assert (
        out["url"] is None
        and out["credentials"] is None
        and out["static_headers"] is None
        and out["env"] is None
    )


def test_mcp_normalize_row_shape():
    row = mcpc.normalize(SERVERS[3], "healthy")
    assert row == {
        "server_id": "db-ha-0001",
        "name": "ha",
        "server_name": "home_assistant",
        "source": "db",
        "transport": "http",
        "auth_type": "bearer_token",
        "url": "https://ha.example:8123/…",
        "access_groups": ["home"],
        "allowed_tools": [],
        "allow_all_keys": False,
        "teams": [],
        "approval_status": "active",
        "status": "healthy",
        "description": "home_assistant tools",
    }
    assert mcpc.normalize(SERVERS[0])["source"] == "config"


def test_mcp_normalize_team_names():
    s = mcp_server(
        "t",
        "t",
        teams=[{"team_id": "team-eng", "team_alias": "eng"}, {"team_id": "team-x", "team_alias": None}],
    )
    assert mcpc.normalize(s)["teams"] == ["eng", "team-x"]


def test_mcp_resolve_server_by_id_alias_name_and_case():
    assert mcpc.resolve_server(SERVERS, "db-ha-0001")["by"] == "server_id"
    assert mcpc.resolve_server(SERVERS, "ha")["server"]["server_id"] == "db-ha-0001"
    assert mcpc.resolve_server(SERVERS, "home_assistant")["server"]["server_id"] == "db-ha-0001"
    assert mcpc.resolve_server(SERVERS, "Grafana")["server"]["server_name"] == "grafana"
    assert mcpc.resolve_server(SERVERS, "nope") is None and mcpc.resolve_server(SERVERS, "") is None


def test_mcp_resolve_server_refuses_an_ambiguous_name():
    dup = [mcp_server("a", "docs", is_config=True), mcp_server("b", "docs")]
    with pytest.raises(ValueError, match="2 MCP servers"):
        mcpc.resolve_server(dup, "docs")


def test_mcp_new_server_body_minimal_defaults_transport_http():
    assert mcpc.new_server("docs", url="https://mcp.example/mcp") == {
        "server_name": "docs",
        "transport": "http",
        "url": "https://mcp.example/mcp",
    }


def test_mcp_new_server_full_body():
    body = mcpc.new_server(
        "home_assistant",
        url="https://ha/mcp",
        alias="ha",
        server_id="srv-ha",
        description="HA",
        auth_type="bearer_token",
        auth_value="tok",
        access_groups=["home"],
        allowed_tools=["light_turn_on"],
        allow_all_keys=True,
        transport="sse",
    )
    assert body == {
        "server_id": "srv-ha",
        "server_name": "home_assistant",
        "alias": "ha",
        "description": "HA",
        "transport": "sse",
        "url": "https://ha/mcp",
        "auth_type": "bearer_token",
        "mcp_access_groups": ["home"],
        "allowed_tools": ["light_turn_on"],
        "allow_all_keys": True,
        "credentials": {"auth_value": "tok"},
    }


@pytest.mark.parametrize(
    "kw,msg",
    [
        ({"name": "home-assistant", "url": "https://x"}, "reserves"),
        ({"name": "ok", "alias": "a-b", "url": "https://x"}, "--alias"),
        ({"name": "ok", "url": None}, "--url is required"),
        ({"name": "ok", "url": "https://x", "transport": "stdio"}, "--transport"),
        ({"name": "ok", "url": "https://x", "server_id": "all-proxy-mcpservers"}, "reserved"),
        ({"name": "ok", "url": "https://x", "auth_type": "magic"}, "--auth-type"),
        ({"name": "ok", "url": "https://x", "auth_value": "tok"}, "needs --auth-type"),
        ({"name": "", "url": "https://x"}, "--name"),
    ],
)
def test_mcp_new_server_refuses_what_the_proxy_would_400(kw, msg):
    name = kw.pop("name")
    with pytest.raises(ValueError, match=msg):
        mcpc.new_server(name, **kw)


def test_mcp_update_server_sends_only_given_fields_and_can_clear_lists():
    assert mcpc.update_server("s1", access_groups=["home"]) == {
        "server_id": "s1",
        "mcp_access_groups": ["home"],
    }
    assert mcpc.update_server("s1", allowed_tools=[]) == {"server_id": "s1", "allowed_tools": []}
    assert mcpc.update_server("s1", allow_all_keys=False) == {"server_id": "s1", "allow_all_keys": False}
    assert mcpc.update_server("s1", auth_value="t")["credentials"] == {"auth_value": "t"}
    with pytest.raises(ValueError, match="nothing to update"):
        mcpc.update_server("s1")
    with pytest.raises(ValueError, match="--alias"):
        mcpc.update_server("s1", alias="a-b")


def test_mcp_split_csv():
    assert mcpc.split_csv(" a, b,,c ") == ["a", "b", "c"]
    assert mcpc.split_csv("") == [] and mcpc.split_csv(None) is None


def test_mcp_secret_from_env():
    assert mcpc.secret_from_env("T", {"T": "v"}) == "v"
    assert mcpc.secret_from_env(None, {}) is None
    with pytest.raises(ValueError, match="unset or empty"):
        mcpc.secret_from_env("T", {"T": ""})


def test_mcp_masked_body_hides_the_credential():
    body = {"server_name": "x", "credentials": {"auth_value": "SECRET"}}
    shown = mcpc.masked_body(body, "HA_TOKEN")
    assert "SECRET" not in json.dumps(shown) and shown["credentials"] == {
        "auth_value": "**** (from $HA_TOKEN)"
    }
    assert body["credentials"]["auth_value"] == "SECRET"  # the real body is untouched
    assert mcpc.masked_body({"a": 1}) == {"a": 1}


def test_mcp_tool_server_longest_label_wins():
    labels = ["ha", "ha_admin", "grafana"]
    assert mcpc.tool_server("ha_admin-reboot", labels) == "ha_admin"
    assert mcpc.tool_server("ha-light_turn_on", labels) == "ha"
    assert mcpc.tool_server("other-thing", labels) == "other"
    assert mcpc.tool_server("plain", labels) is None


def test_mcp_normalize_tool():
    labels = [mcpc.server_label(s) for s in SERVERS]
    rows = [mcpc.normalize_tool(t, labels) for t in TOOLS["tools"]]
    ha = rows[-1]
    assert ha == {
        "name": "ha-light_turn_on",
        "server": "ha",
        "tool": "light_turn_on",
        "title": None,
        "read_only": False,
        "destructive": True,
        "description": "Turn a light on.",
    }
    assert rows[0]["server"] == "context7" and rows[0]["tool"] == "resolve-library-id"


def test_mcp_key_mcp_and_permission_body():
    k = key("svc", permission(groups=["devtools"], servers=["db-ha-0001"]))
    assert mcpc.key_mcp(k) == {
        "access_groups": ["devtools"],
        "servers": ["db-ha-0001"],
        "tool_permissions": None,
    }
    assert mcpc.key_mcp(key("bare")) == {"access_groups": [], "servers": [], "tool_permissions": None}
    body = mcpc.permission_body(permission(groups=["devtools"], vector_stores=["vs1"]))
    assert body == {"mcp_access_groups": ["devtools"], "vector_stores": ["vs1"]}  # no row id, no nulls
    assert mcpc.permission_body(permission()) is None and mcpc.permission_body(None) is None


def test_mcp_effective_servers():
    open_srv = [*SERVERS, mcp_server("pub", "public_docs", groups=(), allow_all_keys=True)]
    g = mcpc.key_mcp(key("k", permission(groups=["home"])))
    assert mcpc.effective_servers(g, open_srv) == ["ha", "public_docs"]
    g = mcpc.key_mcp(key("k", permission(servers=["grafana"])))
    assert mcpc.effective_servers(g, SERVERS) == ["grafana"]
    assert mcpc.effective_servers(mcpc.key_mcp(key("k")), SERVERS) is None  # inherited
    g = mcpc.key_mcp(key("k", permission(groups=["home"], servers=["no-mcp-servers"])))
    assert mcpc.effective_servers(g, open_srv) == []  # the opt-out sentinel wins


def test_mcp_grant_permission_resolves_and_validates():
    known = GROUPS["access_groups"]
    assert mcpc.grant_permission(SERVERS, known, None, None) is None
    assert mcpc.grant_permission(SERVERS, known, "home", None) == {"mcp_access_groups": ["home"]}
    assert mcpc.grant_permission(SERVERS, known, "", None) == {"mcp_access_groups": []}
    assert mcpc.grant_permission(SERVERS, known, None, "ha,grafana") == {
        "mcp_servers": ["db-ha-0001", "222c4d8eceea436fb9b4e52a56eae967"]
    }
    assert mcpc.grant_permission(SERVERS, known, None, "none") == {"mcp_servers": ["no-mcp-servers"]}
    with pytest.raises(ValueError, match=r"unknown MCP access group.*homee"):
        mcpc.grant_permission(SERVERS, known, "homee", None)
    with pytest.raises(ValueError, match="no MCP server matches 'nope'"):
        mcpc.grant_permission(SERVERS, known, None, "nope")


def test_mcp_keys_granting_matches_id_or_name():
    ks = [
        key("a", permission(servers=["db-ha-0001"])),
        key("b", permission(servers=["ha"])),
        key("c", permission(groups=["home"])),
        key("d"),
    ]
    assert [k["key_alias"] for k in mcpc.keys_granting(ks, SERVERS[3])] == ["a", "b"]


def test_mcp_server_path_escapes_the_id():
    assert mcpc.server_path("db-ha-0001") == "/v1/mcp/server/db-ha-0001"
    assert mcpc.server_path("a/b c") == "/v1/mcp/server/a%2Fb%20c"


# ── CLI over the mocked proxy ─────────────────────────────────────────────

HA = "db-ha-0001"
GRAFANA = "222c4d8eceea436fb9b4e52a56eae967"


def proxy(keys=(), servers=None, statuses=None, tools=None, info=None):
    """A GET handler for the MCP endpoints plus /key/list."""
    servers = SERVERS if servers is None else servers
    calls = []

    def get(c, p, **k):
        calls.append((p, k.get("params")))
        if p == "/v1/mcp/server":
            return servers
        if p == "/v1/mcp/server/health":
            want = (k.get("params") or {}).get("server_ids")
            st = statuses or {s["server_id"]: "healthy" for s in servers}
            return health({i: v for i, v in st.items() if not want or i in want})
        if p.startswith("/v1/mcp/server/"):
            return info if info is not None else next(s for s in servers if p.endswith(s["server_id"]))
        if p == "/v1/mcp/tools":
            return tools if tools is not None else TOOLS
        if p == "/v1/mcp/access_groups":
            return GROUPS
        if p == "/v1/mcp/sessions":
            return SESSIONS
        if p == "/key/list":
            return {"keys": list(keys), "total_pages": 1}
        return {}

    get.calls = calls
    return get


def test_mcp_list_json_rows_and_no_secret():
    r = run(["--json", "mcp", "list"], get=proxy())
    rows = json.loads(r.output)
    assert r.exit_code == 0 and [x["name"] for x in rows] == ["context7", "grafana", "kubernetes", "ha"]
    assert [x["source"] for x in rows] == ["config", "config", "config", "db"]
    assert rows[3]["url"] == "https://ha.example:8123/…" and "SECRET" not in r.output
    assert rows[0]["status"] is None  # no --health: no connection to any server


def test_mcp_list_health_merges_status_and_human_table():
    get = proxy(statuses={GRAFANA: "unhealthy"})
    r = run(["mcp", "list", "--health"], get=get)
    assert r.exit_code == 0 and "SERVER" in r.output and "unhealthy" in r.output
    assert ("/v1/mcp/server/health", None) in get.calls


def test_mcp_info_resolves_alias_and_redacts():
    full = {**SERVERS[3], "credentials": {"auth_value": "SECRET-CRED"}, "status": "healthy"}
    get = proxy(info=full)
    r = run(["mcp", "info", "ha"], get=get)
    out = json.loads(r.output)
    assert r.exit_code == 0 and out["server_id"] == HA and out["status"] == "healthy"
    assert "SECRET" not in r.output and out["credentials"] == ["auth_value"]
    assert ("/v1/mcp/server/db-ha-0001", None) in get.calls


def test_mcp_info_unknown_ref_points_at_list():
    r = run(["mcp", "info", "nope"], get=proxy())
    assert r.exit_code != 0 and "mcp list" in r.output


def test_mcp_health_exit_2_names_the_unhealthy_first():
    r = run(["--json", "mcp", "health"], get=proxy(statuses={HA: "healthy", GRAFANA: "unhealthy"}))
    out = json.loads(r.output)
    assert r.exit_code == 2 and out["healthy"] is False and out["unhealthy"] == ["grafana"]
    assert out["servers"][0]["name"] == "grafana"


def test_mcp_health_named_servers_sends_their_ids():
    get = proxy()
    r = run(["--json", "mcp", "health", "ha", "grafana"], get=get)
    assert r.exit_code == 0 and json.loads(r.output)["healthy"] is True
    assert ("/v1/mcp/server/health", {"server_ids": [HA, GRAFANA]}) in get.calls


def test_mcp_tools_groups_by_server_and_filters():
    r = run(["--json", "mcp", "tools"], get=proxy())
    out = json.loads(r.output)
    assert out["count"] == 5 and out["by_server"] == {"context7": 2, "grafana": 1, "ha": 1, "kubernetes": 1}
    r = run(["--json", "mcp", "tools", "--server", "home_assistant"], get=proxy())
    out = json.loads(r.output)
    assert [t["tool"] for t in out["tools"]] == ["light_turn_on"] and out["tools"][0]["read_only"] is False


def test_mcp_tools_human_table():
    r = run(["mcp", "tools"], get=proxy())
    assert r.exit_code == 0 and "5 tool(s)" in r.output and "light_turn_on" in r.output


def test_mcp_groups_lists_members_and_open_servers():
    servers = [*SERVERS, mcp_server("pub", "public_docs", groups=(), allow_all_keys=True)]
    r = run(["--json", "mcp", "groups"], get=proxy(servers=servers))
    out = json.loads(r.output)
    assert out["groups"] == [
        {"group": "devtools", "servers": ["context7", "grafana", "kubernetes"]},
        {"group": "home", "servers": ["ha"]},
    ]
    assert out["allow_all_keys"] == ["public_docs"]


def test_mcp_access_maps_keys_to_servers_and_filters():
    keys = [
        key("cloudroom", permission(groups=["devtools"])),
        key("rufus", permission(groups=["devtools", "home"])),
        key("ha-only", permission(servers=[HA])),
        key("team-key", team="team-eng"),
    ]
    r = run(["--json", "mcp", "access"], get=proxy(keys=keys))
    rows = {x["key_alias"]: x for x in json.loads(r.output)["keys"]}
    assert rows["cloudroom"]["reaches"] == ["context7", "grafana", "kubernetes"]
    assert rows["rufus"]["reaches"] == ["context7", "grafana", "ha", "kubernetes"]
    assert rows["ha-only"]["servers"] == ["ha"] and rows["ha-only"]["reaches"] == ["ha"]
    assert rows["team-key"]["reaches"] is None and rows["team-key"]["inherits_from"] == ["team:team-eng"]
    r = run(["--json", "mcp", "access", "--server", "ha"], get=proxy(keys=keys))
    assert [x["key_alias"] for x in json.loads(r.output)["keys"]] == ["rufus", "ha-only"]
    r = run(["mcp", "access"], get=proxy(keys=keys))
    assert "(inherited: team:team-eng)" in r.output


def _users_get(users, keys, servers=None):
    """proxy() plus /user/get_users (``get_users`` in internal_user_endpoints.py:
    ``{"users": [...], "total": n, "page": p, "page_size": s, "total_pages": t}``)."""
    base = proxy(keys=keys, servers=servers)

    def get(c, p, **k):
        if p == "/user/get_users":
            return {"users": users, "total": len(users), "page": 1, "page_size": 100, "total_pages": 1}
        return base(c, p, **k)

    return get


def test_mcp_access_default_deny_key_shows_none_not_inherited():
    """Reviewer pin (round 1): no grant, no team, no org, no admin owner → the
    gateway answers 403 "The key has no MCP servers granted"
    (_raise_if_initialize_grants_no_mcp_servers, mcp_server/operations.py)."""
    k = {**key("orphan"), "user_id": "bob"}
    users = [{"user_id": "bob", "user_role": "internal_user"}]
    r = run(["--json", "mcp", "access"], get=_users_get(users, [k]))
    row = json.loads(r.output)["keys"][0]
    assert row["reaches"] == [] and row["inherits_from"] == []
    r = run(["mcp", "access"], get=_users_get(users, [k]))
    assert "(none)" in r.output and "inherited" not in r.output


def test_mcp_access_default_deny_key_still_reaches_allow_all_keys_servers():
    servers = [*SERVERS, mcp_server("pub", "public_docs", groups=(), allow_all_keys=True)]
    r = run(["--json", "mcp", "access"], get=_users_get([], [key("orphan")], servers=servers))
    assert json.loads(r.output)["keys"][0]["reaches"] == ["public_docs"]


def test_mcp_access_admin_owner_or_org_keeps_inherited():
    admin = {**key("admin-key"), "user_id": "jon"}
    org = {**key("org-key"), "organization_id": "org-1"}
    users = [{"user_id": "jon", "user_role": "proxy_admin"}]
    rows = {
        x["key_alias"]: x
        for x in json.loads(run(["--json", "mcp", "access"], get=_users_get(users, [admin, org])).output)[
            "keys"
        ]
    }
    assert rows["admin-key"]["reaches"] is None and rows["admin-key"]["inherits_from"] == ["role:proxy_admin"]
    assert rows["org-key"]["inherits_from"] == ["org:org-1"]


def test_mcp_access_unreadable_users_does_not_claim_admin():
    from cli_anything.litellm.core import backend as be

    k = {**key("orphan"), "user_id": "bob"}
    base = proxy(keys=[k])

    def get(c, p, **kw):
        if p == "/user/get_users":
            raise be.ApiError(403, "forbidden", "http://p" + p)
        return base(c, p, **kw)

    r = run(["--json", "mcp", "access"], get=get)
    assert r.exit_code == 0 and json.loads(r.output)["keys"][0]["reaches"] == []


def test_mcp_inherit_sources_and_reach_label():
    assert mcpc.inherit_sources({"team_id": "t"}, {}) == ["team:t"]
    assert mcpc.inherit_sources({"user_id": "u"}, {"u": "proxy_admin_viewer"}) == ["role:proxy_admin_viewer"]
    assert mcpc.inherit_sources({"user_id": "u"}, {"u": "internal_user"}) == []
    assert mcpc.reach_label(None, ["team:t"]) == "(inherited: team:t)"
    assert mcpc.reach_label([], []) == "(none)" and mcpc.reach_label(["a", "b"], []) == "a,b"


def test_mcp_add_dry_run_masks_credential_and_sends_nothing(monkeypatch):
    monkeypatch.setenv("HA_MCP_TOKEN", "SECRET-TOKEN")
    post = mock.Mock()
    r = run(
        [
            "--dry-run",
            "mcp",
            "add",
            "--name",
            "home_assistant",
            "--alias",
            "ha",
            "--url",
            "https://ha/api/mcp",
            "--auth-type",
            "bearer_token",
            "--auth-value-env",
            "HA_MCP_TOKEN",
            "--access-groups",
            "home",
        ],
        post=post,
    )
    out = json.loads(r.output)
    assert r.exit_code == 0 and out["would"] == "POST /v1/mcp/server"
    assert out["body"]["credentials"] == {"auth_value": "**** (from $HA_MCP_TOKEN)"}
    assert out["body"]["transport"] == "http" and out["body"]["mcp_access_groups"] == ["home"]
    assert "SECRET" not in r.output
    post.assert_not_called()


def test_mcp_add_posts_real_secret_and_prints_redacted_row(monkeypatch):
    monkeypatch.setenv("HA_MCP_TOKEN", "SECRET-TOKEN")
    created = {**SERVERS[3], "credentials": {"auth_value": "SECRET-TOKEN"}}
    post = mock.Mock(return_value=created)
    r = run(
        [
            "mcp",
            "add",
            "--name",
            "home_assistant",
            "--url",
            "https://ha/api/mcp",
            "--auth-type",
            "bearer_token",
            "--auth-value-env",
            "HA_MCP_TOKEN",
        ],
        post=post,
    )
    assert r.exit_code == 0 and "added MCP server ha (db-ha-0001)" in r.output and "SECRET" not in r.output
    path, body = post.call_args.args[1], post.call_args.args[2]
    assert path == "/v1/mcp/server" and body["credentials"] == {"auth_value": "SECRET-TOKEN"}


def test_mcp_add_refuses_before_the_proxy():
    post = mock.Mock()
    r = run(["mcp", "add", "--name", "home-assistant", "--url", "https://x"], post=post)
    assert r.exit_code != 0 and "reserves" in r.output
    r = run(["mcp", "add", "--name", "x"], post=post)
    assert r.exit_code != 0 and "--url is required" in r.output
    r = run(
        [
            "mcp",
            "add",
            "--name",
            "x",
            "--url",
            "https://x",
            "--auth-type",
            "bearer_token",
            "--auth-value-env",
            "UNSET_VAR_XYZ",
        ],
        post=post,
    )
    assert r.exit_code != 0 and "UNSET_VAR_XYZ" in r.output
    post.assert_not_called()


def test_mcp_update_puts_partial_body():
    put = mock.Mock(return_value=SERVERS[3])
    with mock.patch("cli_anything.litellm.core.backend.put", put):
        r = run(
            ["mcp", "update", "ha", "--access-groups", "home,devtools", "--no-allow-all-keys"], get=proxy()
        )
    assert r.exit_code == 0
    assert put.call_args.args[1] == "/v1/mcp/server"
    assert put.call_args.args[2] == {
        "server_id": HA,
        "mcp_access_groups": ["home", "devtools"],
        "allow_all_keys": False,
    }


def test_mcp_update_dry_run_and_nothing_to_update():
    r = run(["--dry-run", "mcp", "update", "ha", "--allowed-tools", ""], get=proxy())
    out = json.loads(r.output)
    assert out["would"] == "PUT /v1/mcp/server" and out["body"] == {"server_id": HA, "allowed_tools": []}
    r = run(["mcp", "update", "ha"], get=proxy())
    assert r.exit_code != 0 and "nothing to update" in r.output


def test_mcp_update_and_delete_refuse_config_servers():
    for args in (["mcp", "update", "grafana", "--description", "x"], ["mcp", "delete", "grafana", "--yes"]):
        r = run(args, get=proxy())
        assert r.exit_code != 0 and "config.yaml" in r.output


def test_mcp_delete_refuses_without_yes_off_tty():
    dele = mock.Mock()
    with mock.patch("cli_anything.litellm.core.backend.delete", dele):
        r = run(["mcp", "delete", "ha"], get=proxy())
    assert r.exit_code != 0 and "without --yes" in r.output
    dele.assert_not_called()


def test_mcp_delete_dry_run_then_yes_names_stale_grants():
    keys = [key("rufus", permission(servers=[HA])), key("other", permission(groups=["devtools"]))]
    r = run(["--dry-run", "mcp", "delete", "ha"], get=proxy(keys=keys))
    assert json.loads(r.output)["would"] == "DELETE /v1/mcp/server/db-ha-0001"
    dele = mock.Mock(return_value=None)  # 202, empty body
    with mock.patch("cli_anything.litellm.core.backend.delete", dele):
        r = run(["--json", "mcp", "delete", "ha", "--yes"], get=proxy(keys=keys))
    out = json.loads(r.output)
    assert r.exit_code == 0 and dele.call_args.args[1] == "/v1/mcp/server/db-ha-0001"
    assert out == {"deleted": HA, "name": "ha", "keys_with_stale_grant": ["rufus"]}


def test_mcp_sessions_json_and_human():
    r = run(["--json", "mcp", "sessions"], get=proxy())
    assert json.loads(r.output)["total_sessions"] == 2
    r = run(["mcp", "sessions"], get=proxy())
    assert "worker 41: 2 session(s)" in r.output and "claude-code" in r.output


def test_mcp_sessions_close_validates_and_gates():
    r = run(["mcp", "sessions-close"])
    assert r.exit_code != 0 and "--prefix and/or --user" in r.output
    r = run(["mcp", "sessions-close", "--prefix", "a1b2"])
    assert r.exit_code != 0 and "at least 8" in r.output
    r = run(["--dry-run", "mcp", "sessions-close", "--user", "jon"])
    assert json.loads(r.output) == {
        "dry_run": True,
        "would": "DELETE /v1/mcp/sessions",
        "params": {"user_id": "jon"},
    }
    dele = mock.Mock()
    with mock.patch("cli_anything.litellm.core.backend.delete", dele):
        r = run(["mcp", "sessions-close", "--user", "jon"])
    assert r.exit_code != 0 and "without --yes" in r.output
    dele.assert_not_called()


def test_mcp_sessions_close_yes_sends_params():
    dele = mock.Mock(return_value=terminated(SESSIONS["sessions"][:1]))
    with mock.patch("cli_anything.litellm.core.backend.delete", dele):
        r = run(["mcp", "sessions-close", "--prefix", "a1b2c3d4", "--yes"])
    assert r.exit_code == 0 and "closed 1 session(s)" in r.output
    assert dele.call_args.args[1] == "/v1/mcp/sessions" and dele.call_args.kwargs["params"] == {
        "session_id_prefix": "a1b2c3d4"
    }


def test_mcp_sessions_403_for_non_admin_is_a_clean_error():
    from cli_anything.litellm.core import backend as be

    def get(c, p, **k):
        raise be.ApiError(403, "Admin access required to view MCP gateway sessions.", "http://p" + p)

    r = run(["mcp", "sessions"], get=get)
    assert r.exit_code != 0 and "403" in r.output and "Traceback" not in r.output


# ── keys --mcp-groups / --mcp-servers ─────────────────────────────────────


def test_keys_generate_mcp_flags_build_object_permission():
    post = mock.Mock()
    r = run(
        [
            "--dry-run",
            "keys",
            "generate",
            "--alias",
            "svc",
            "--mcp-groups",
            "devtools",
            "--mcp-servers",
            "ha",
        ],
        get=proxy(),
        post=post,
    )
    body = json.loads(r.output)["body"]
    assert body["object_permission"] == {"mcp_access_groups": ["devtools"], "mcp_servers": [HA]}
    post.assert_not_called()


def test_keys_generate_unknown_group_refused_before_the_proxy():
    post = mock.Mock()
    r = run(["keys", "generate", "--alias", "svc", "--mcp-groups", "devtool"], get=proxy(), post=post)
    assert r.exit_code != 0 and "unknown MCP access group" in r.output and "devtools" in r.output
    post.assert_not_called()


def test_keys_update_mcp_only_is_enough_and_none_opts_out():
    keys = [key("svc")]
    post = mock.Mock(return_value={})
    r = run(["keys", "update", "svc", "--mcp-servers", "none"], get=proxy(keys=keys), post=post)
    assert r.exit_code == 0
    assert post.call_args.args[1] == "/key/update"
    assert post.call_args.args[2] == {
        "object_permission": {"mcp_servers": ["no-mcp-servers"]},
        "key": keys[0]["token"],
    }


def test_keys_list_json_carries_the_mcp_grant():
    keys = [key("cloudroom", permission(groups=["devtools"]))]
    r = run(["--json", "keys", "list"], get=proxy(keys=keys))
    assert json.loads(r.output)[0]["mcp"] == {
        "access_groups": ["devtools"],
        "servers": [],
        "tool_permissions": None,
    }


def test_keys_rotate_carries_the_mcp_grant_without_the_row_id():
    k = key("cloudroom", permission(groups=["devtools"]))

    def get(c, p, **kw):
        if p == "/key/info":  # info_key_fn: {"key", "info"} with object_permission attached
            return {"key": k["token"], "info": {**k, "token": None}}
        return proxy(keys=[k])(c, p, **kw)

    r = run(["--dry-run", "keys", "rotate", "cloudroom"], get=get)
    gen = json.loads(r.output)["steps"][1]["body"]
    assert gen["object_permission"] == {"mcp_access_groups": ["devtools"]}
    assert "object_permission_id" not in json.dumps(gen)


# ── workflows ─────────────────────────────────────────────────────────────


def test_workflow_add_server_grant_key_audit_then_delete(monkeypatch):
    """Onboard an MCP server: add → health → grant a key by group → `mcp access`
    shows it → delete names the key still holding an explicit grant."""
    monkeypatch.setenv("TOK", "SECRET-TOKEN")
    state = {"servers": list(SERVERS[:3]), "keys": [key("cloudroom", permission(groups=["devtools"]))]}
    new_row = mcp_server("db-docs-1", "docs", url="https://docs.example/mcp", groups=("home",))

    def get(c, p, **k):
        return proxy(keys=state["keys"], servers=state["servers"])(c, p, **k)

    def post(c, p, body=None, **k):
        if p == "/v1/mcp/server":
            state["servers"].append(new_row)
            return new_row
        if p == "/key/update":
            for kk in state["keys"]:
                if kk["token"] == body["key"]:
                    op = kk["object_permission"] or permission()
                    kk["object_permission"] = {
                        **op,
                        **body["object_permission"],
                    }  # the proxy merges per field
            return {}
        return {}

    r = run(
        [
            "mcp",
            "add",
            "--name",
            "docs",
            "--url",
            "https://docs.example/mcp",
            "--access-groups",
            "home",
            "--auth-type",
            "bearer_token",
            "--auth-value-env",
            "TOK",
        ],
        get=get,
        post=post,
    )
    assert r.exit_code == 0 and "SECRET" not in r.output
    assert run(["mcp", "health", "docs"], get=get).exit_code == 0
    r = run(["keys", "update", "cloudroom", "--mcp-servers", "docs"], get=get, post=post)
    assert r.exit_code == 0
    access = json.loads(run(["--json", "mcp", "access", "--server", "docs"], get=get).output)["keys"]
    assert access[0]["key_alias"] == "cloudroom" and "docs" in access[0]["reaches"]
    assert "context7" in access[0]["reaches"]  # the merge kept the devtools group
    dele = mock.Mock(return_value=None)
    with mock.patch("cli_anything.litellm.core.backend.delete", dele):
        out = json.loads(run(["--json", "mcp", "delete", "docs", "--yes"], get=get).output)
    assert out["keys_with_stale_grant"] == ["cloudroom"]


def test_workflow_unhealthy_server_to_affected_keys():
    """Incident triage: `mcp health` exits 2 naming grafana → `mcp access
    --server grafana` names the keys that lose those tools."""
    keys = [key("cloudroom", permission(groups=["devtools"])), key("ha-bot", permission(groups=["home"]))]
    get = proxy(keys=keys, statuses={GRAFANA: "unhealthy", HA: "healthy"})
    r = run(["--json", "mcp", "health"], get=get)
    assert r.exit_code == 2
    bad = json.loads(r.output)["unhealthy"][0]
    affected = json.loads(run(["--json", "mcp", "access", "--server", bad], get=get).output)["keys"]
    assert [k["key_alias"] for k in affected] == ["cloudroom"]


def test_backend_put_and_delete_send_the_method_body_params_and_auth():
    from cli_anything.litellm.core import backend as be

    resp = mock.Mock(status_code=202, content=b"")
    conn = {"url": "http://p", "key": "sk-master", "verify": True, "timeout": 5}
    with mock.patch.object(be.requests, "request", return_value=resp) as req:
        assert be.delete(conn, "/v1/mcp/sessions", params={"user_id": "jon"}) is None
        be.put(conn, "/v1/mcp/server", {"server_id": "s"})
    (m1, u1), k1 = req.call_args_list[0].args, req.call_args_list[0].kwargs
    (m2, _), k2 = req.call_args_list[1].args, req.call_args_list[1].kwargs
    assert (
        (m1, u1) == ("DELETE", "http://p/v1/mcp/sessions")
        and k1["params"] == {"user_id": "jon"}
        and k1["json"] is None
    )
    assert (
        m2 == "PUT"
        and k2["json"] == {"server_id": "s"}
        and k2["headers"]["Authorization"] == "Bearer sk-master"
    )


# ── the installed command (HARNESS: subprocess via _resolve_cli) ──────────


def _resolve_cli(name):
    """Resolve installed CLI command; falls back to python -m for dev.

    Set env CLI_ANYTHING_FORCE_INSTALLED=1 to require the installed command.
    """
    import os
    import shutil
    import sys

    force = os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1"
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH. Install with: pip install -e .")
    print(f"[_resolve_cli] Falling back to: {sys.executable} -m cli_anything.litellm")
    return [sys.executable, "-m", "cli_anything.litellm"]


class TestMcpSubprocess:
    CLI_BASE = _resolve_cli("cli-anything-litellm")

    def _run(self, args, env_extra=None):
        import os
        import subprocess

        env = {
            **os.environ,
            "LITELLM_URL": "http://127.0.0.1:9",
            "LITELLM_API_KEY": "sk-test",
            **(env_extra or {}),
        }
        return subprocess.run(self.CLI_BASE + args, capture_output=True, text=True, env=env, timeout=60)

    def test_mcp_help_lists_every_verb(self):
        r = self._run(["mcp", "--help"])
        assert r.returncode == 0
        for verb in (
            "list",
            "info",
            "health",
            "tools",
            "groups",
            "access",
            "add",
            "update",
            "delete",
            "sessions",
        ):
            assert verb in r.stdout

    def test_mcp_add_dry_run_json_never_leaks_the_env_secret(self):
        r = self._run(
            [
                "--json",
                "--dry-run",
                "mcp",
                "add",
                "--name",
                "docs",
                "--url",
                "https://docs.example/mcp",
                "--auth-type",
                "bearer_token",
                "--auth-value-env",
                "DOCS_TOKEN",
            ],
            env_extra={"DOCS_TOKEN": "SECRET-SUBPROCESS"},
        )
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["would"] == "POST /v1/mcp/server" and out["body"]["server_name"] == "docs"
        assert "SECRET" not in r.stdout + r.stderr

    def test_mcp_list_unreachable_proxy_fails_loudly(self):
        r = self._run(["mcp", "list"])
        assert r.returncode != 0 and "127.0.0.1:9" in (r.stdout + r.stderr) and "Traceback" not in r.stderr


def test_users_list_falls_back_to_user_list_on_v1_106():
    """v1.106.3 serves GET /user/list (UserListResponse, internal_user_endpoints.get_users);
    /user/get_users 404s there."""
    from cli_anything.litellm.core import backend as be

    seen = []

    def get(c, p, **k):
        seen.append((p, k.get("params")))
        if p == "/user/get_users":
            raise be.ApiError(404, "Not Found", "http://p" + p)
        if p == "/user/list":
            return {
                "users": [{"user_id": "jon", "user_role": "proxy_admin", "teams": []}],
                "total": 1,
                "page": 1,
                "page_size": 100,
                "total_pages": 1,
            }
        return {}

    r = run(["--json", "users", "list"], get=get)
    assert r.exit_code == 0 and json.loads(r.output)[0]["user_id"] == "jon"
    assert ("/user/list", {"page": 1, "page_size": 100}) in seen
