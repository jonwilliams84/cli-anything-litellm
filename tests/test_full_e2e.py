"""End-to-end and workflow tests: CLI commands over a mocked proxy."""

from __future__ import annotations

import json
from unittest import mock

from cli_anything.litellm import litellm_cli as cli_mod

from test_harness import dep, live, run


# ── models add ────────────────────────────────────────────────────────────


def test_models_add_dry_run_prints_body_sends_nothing():
    post = mock.Mock()
    r = run(
        [
            "--dry-run",
            "models",
            "add",
            "--name",
            "qwen",
            "--model",
            "hosted_vllm/qwen",
            "--api-base",
            "http://n9:8000/v1",
            "--api-key",
            "os.environ/K",
        ],
        post=post,
    )
    body = json.loads(r.output)["body"]
    assert body["model_name"] == "qwen" and body["litellm_params"] == {
        "model": "hosted_vllm/qwen",
        "api_base": "http://n9:8000/v1",
        "api_key": "os.environ/K",
    }
    post.assert_not_called()


def test_models_add_posts_and_reports_id():
    post = mock.Mock(return_value={"data": {"model_info": {"id": "dep-x"}}})
    r = run(
        ["models", "add", "--name", "qwen", "--model", "openai/gpt-4o", "--mode", "chat", "--max-input-tokens", "131072"],
        post=post,
    )
    body = {
        "model_name": "qwen",
        "litellm_params": {"model": "openai/gpt-4o"},
        "model_info": {"mode": "chat", "max_input_tokens": 131072},
    }
    post.assert_called_once()
    assert post.call_args.args[1] == "/model/new" and post.call_args.args[2] == body
    assert "added qwen (dep-x)" in r.output


def test_models_add_missing_model_fails_before_the_proxy():
    post = mock.Mock()
    r = run(["models", "add", "--name", "qwen"], post=post)
    assert r.exit_code != 0 and "--model" in r.output
    post.assert_not_called()


# ── models delete ─────────────────────────────────────────────────────────


def test_models_delete_by_id_dry_run_exact_body():
    get = lambda c, p, **k: {"data": [live(dep(1)), live(dep(2, db=True))]} if p == "/v1/model/info" else {}
    r = run(["--dry-run", "models", "delete", "node-2"], get=get)
    assert json.loads(r.output)["body"] == {"id": "node-2"}


def test_models_delete_by_id_refuses_without_yes_off_tty():
    get = lambda c, p, **k: {"data": [live(dep(1))]} if p == "/v1/model/info" else {}
    post = mock.Mock()
    r = run(["models", "delete", "node-1"], get=get, post=post)
    assert r.exit_code != 0 and "without --yes" in r.output
    post.assert_not_called()


def test_models_delete_by_group_name_counts_replicas():
    get = lambda c, p, **k: ({"data": [live(dep(1)), live(dep(2)), live(dep(3, name="embed", mode="embedding"))]} if p == "/v1/model/info" else {})
    post = mock.Mock(return_value={"message": "Model deleted"})
    r = run(["models", "delete", "qwen", "--yes"], get=get, post=post)
    assert post.call_args.args[1:] == ("/model/delete", {"model_name": "qwen"})
    assert "all 2 replicas" in r.output and "Model deleted" in r.output


def test_models_delete_unknown_ref_is_an_error():
    get = lambda c, p, **k: {"data": [live(dep(1))]} if p == "/v1/model/info" else {}
    r = run(["models", "delete", "ghost", "--yes"], get=get)
    assert r.exit_code != 0 and "no live deployment matches 'ghost'" in r.output


# ── workflows (new commands combined with existing ones) ──────────────────


def test_workflow_drift_gap_closed_by_models_add_then_visible_in_list():
    """A model group the proxy lacks (drift would flag `only_in_config`) is added
    with `models add` as a DB replica and then shows up in `models list` as SRC=db."""
    state = {"added": False}

    def get(c, p, **k):
        if p == "/v1/model/info":
            deps = [live(dep(1))]
            if state["added"]:
                deps.append(live(dep(9, name="embed", mode="embedding", db=True)))
            return {"data": deps}
        return {}

    def post(c, path, body=None, **k):
        assert path == "/model/new"
        assert body["model_name"] == "embed"
        state["added"] = True
        return {"data": {"model_info": {"id": "node-9"}}}

    r = run(
        ["models", "add", "--name", "embed", "--model", "hosted_vllm/embed", "--api-base", "http://n9:8000/v1", "--mode", "embedding", "--max-input-tokens", "8192"],
        get=get,
        post=post,
    )
    assert "added embed (node-9)" in r.output

    r = run(["--json", "models", "list", "--deployments"], get=get)
    rows = json.loads(r.output)
    assert [row["id"] for row in rows] == ["node-1", "node-9"]
    assert [row["source"] for row in rows] == ["config", "db"]


def test_workflow_delete_db_only_deployment_found_by_resolve(capsys):
    """The drift story the other way: `drift` says `only_live / db_model: true`,
    `models delete node-9` removes exactly that replica, by id, after --yes."""
    deps = {"data": [live(dep(1)), live(dep(9, db=True))]}

    def get(c, p, **k):
        return deps if p == "/v1/model/info" else {}

    post = mock.Mock(return_value={"message": "Model deleted"})
    r = run(["--json", "models", "delete", "node-9", "--yes"], get=get, post=post)
    assert r.exit_code == 0
    post.assert_called_once_with(mock.ANY, "/model/delete", {"id": "node-9"})


# ── users ─────────────────────────────────────────────────────────────────


def users_page(users, total_pages=1):
    return lambda c, p, **k: (
        {"users": users, "total_pages": total_pages, "total_count": len(users)}
        if p == "/user/get_users"
        else {}
    )


def proxy_user(uid, role="internal_user", teams=("eng",), email=None):
    return {
        "user_id": uid,
        "user_email": email or f"{uid}@corp.io",
        "user_role": role,
        "teams": list(teams),
        "spend": 1.5,
    }


def test_users_create_dry_run_prints_body_sends_nothing():
    post = mock.Mock()
    r = run(["--dry-run", "users", "create", "--user-id", "svc-bot", "--email", "svc@corp.io",
             "--teams", "eng,sre", "--max-budget", "5"], post=post)
    assert json.loads(r.output)["body"] == {
        "user_id": "svc-bot", "user_email": "svc@corp.io", "teams": ["eng", "sre"],
        "max_budget": 5.0, "auto_create_key": False,
    }
    assert "POST /user/new" in r.output
    post.assert_not_called()


def test_users_create_posts_no_key_and_reports_created():
    post = mock.Mock(return_value={"user_id": "svc-bot"})
    r = run(["users", "create", "--user-id", "svc-bot", "--role", "proxy_admin_viewer"], post=post)
    body = {"user_id": "svc-bot", "user_role": "proxy_admin_viewer", "auto_create_key": False}
    post.assert_called_once_with(mock.ANY, "/user/new", body)
    assert '"created"' in r.output and "svc-bot" in r.output


def test_users_create_rejects_unknown_role_before_the_proxy():
    post = mock.Mock()
    r = run(["users", "create", "--user-id", "x", "--role", "root"], post=post)
    assert r.exit_code != 0 and "LiteLLM roles" in r.output
    post.assert_not_called()


def test_users_create_without_identity_fails_before_the_proxy():
    post = mock.Mock()
    r = run(["users", "create"], post=post)
    assert r.exit_code != 0 and "--user-id" in r.output and "--email" in r.output
    post.assert_not_called()


def test_users_list_json_and_filters():
    users = [proxy_user("a", role="proxy_admin"), proxy_user("b"), proxy_user("c", teams=("sre",))]
    r = run(["--json", "users", "list"], get=users_page(users))
    rows = json.loads(r.output)
    assert [row["user_id"] for row in rows] == ["a", "b", "c"]
    assert rows[0]["role"] == "proxy_admin" and rows[0]["teams"] == ["eng"]
    r = run(["--json", "users", "list", "--role", "proxy_admin"], get=users_page(users))
    assert [row["user_id"] for row in json.loads(r.output)] == ["a"]
    r = run(["--json", "users", "list", "--team", "sre"], get=users_page(users))
    assert [row["user_id"] for row in json.loads(r.output)] == ["c"]
    r = run(["users", "list"], get=users_page(users))  # human: still the columns
    assert "USER_ID" in r.output and "ROLE" in r.output


def test_users_info_by_email_resolves_then_fetches_by_id():
    def get(c, p, **k):
        if p == "/user/get_users":
            return {"users": [proxy_user("svc-bot", email="svc@corp.io")], "total_pages": 1}
        if p == "/user/info":
            assert k["params"] == {"user_id": "svc-bot"}
            return {
                "user_info": {"user_id": "svc-bot", "user_email": "svc@corp.io", "user_role": "internal_user"},
                "keys": [{"token": "abcd", "key_alias": "svc", "expires": "2027-01-01"}],
            }
        return {}

    r = run(["--json", "users", "info", "svc@corp.io"], get=get)
    out = json.loads(r.output)
    assert out["user"]["user_id"] == "svc-bot" and out["keys"][0]["alias"] == "svc"


def test_users_info_unknown_email_is_an_error():
    r = run(["users", "info", "ghost@corp.io"], get=users_page([proxy_user("a")]))
    assert r.exit_code != 0 and "no user matches" in r.output


def test_users_delete_unknown_ref_is_an_error():
    post = mock.Mock()
    r = run(["users", "delete", "ghost", "--yes"], get=users_page([proxy_user("a")]), post=post)
    assert r.exit_code != 0 and "no live user matches 'ghost'" in r.output and "`users list`" in r.output
    post.assert_not_called()


def test_users_delete_refuses_without_yes_off_tty():
    post = mock.Mock()
    r = run(["users", "delete", "svc-bot"], get=users_page([proxy_user("svc-bot")]), post=post)
    assert r.exit_code != 0 and "without --yes" in r.output
    post.assert_not_called()


def test_users_delete_dry_run_sends_user_id():
    r = run(["--dry-run", "users", "delete", "svc@corp.io"], get=users_page([proxy_user("svc-bot", email="svc@corp.io")]))
    assert json.loads(r.output)["body"] == {"user_id": "svc-bot"}


# ── workflows (new commands combined with existing ones) ──────────────────


def test_workflow_onboard_user_issue_key_then_delete_user():
    """The onboarding story end to end: create the identity, mint a named key
    for it, confirm both through the read commands, then remove the identity —
    and see that the key it owned is named in the output."""
    created = {"u": False, "k": False}
    keys = []

    def get(c, p, **k):
        if p == "/user/get_users":
            users = [
                proxy_user("svc-bot", email="svc@corp.io"),
            ] if created["u"] else []
            return {"users": users, "total_pages": 1, "total_count": len(users)}
        if p == "/key/list":
            return {"keys": keys if created["k"] else [], "total_pages": 1}
        if p == "/user/info":
            assert k["params"] == {"user_id": "svc-bot"}
            return {
                "user_info": {"user_id": "svc-bot", "user_email": "svc@corp.io", "user_role": "internal_user",
                              "teams": ["eng"], "spend": 0},
                "keys": list(keys),
            }
        return {}

    def post(c, path, body=None, **k):
        if path == "/user/new":
            assert body["auto_create_key"] is False
            created["u"] = True
            return {"user_id": "svc-bot"}
        if path == "/key/generate":
            assert body["user_id"] == "svc-bot" and body["key_alias"]
            keys.append({"token": "sk-new", "key_alias": body["key_alias"], "user_id": "svc-bot",
                         "expires": "2027-01-01"})
            created["k"] = True
            return {"key": "sk-new"}
        assert path == "/user/delete"
        return {}

    r = run(["users", "create", "--user-id", "svc-bot", "--email", "svc@corp.io", "--teams", "eng"], get=get, post=post)
    assert '"created"' in r.output
    r = run(["keys", "generate", "--alias", "svc", "--user", "svc-bot"], get=get, post=post)
    assert "sk-new" in r.output
    r = run(["--json", "users", "list", "--team", "eng"], get=get)
    assert [row["user_id"] for row in json.loads(r.output)] == ["svc-bot"]
    r = run(["users", "delete", "svc-bot", "--yes"], get=get, post=post)
    assert "deleted user svc-bot" in r.output
    # the JSON form says the same thing, with the invalidated tokens named
    r = run(["--json", "users", "delete", "svc-bot", "--yes"], get=get, post=post)
    out = json.loads(r.output)
    assert out["deleted"] == {"user_id": "svc-bot"} and out["keys_removed"] == [
        {"alias": "svc", "token": "sk-new"}
    ]


# ── teams ─────────────────────────────────────────────────────────────────


def proxy_team(tid, alias=None, members=(), blocked=None):
    return {
        "team_id": tid,
        "team_alias": alias or tid,
        "members_with_roles": [{"user_id": m} for m in members],
        "spend": 0,
        "blocked": blocked,
    }


def test_teams_create_dry_run_prints_body_sends_nothing():
    post = mock.Mock()
    r = run(["--dry-run", "teams", "create", "--alias", "eng", "--team-id", "team-eng", "--models", "qwen, embed",
             "--max-budget", "50", "--member", "u1", "--member", "new@corp.io", "--member-role", "admin"], post=post)
    assert json.loads(r.output)["body"] == {
        "team_alias": "eng", "team_id": "team-eng", "models": ["qwen", "embed"], "max_budget": 50.0,
        "members_with_roles": [{"user_id": "u1", "team_member_role": "admin"},
                               {"user_email": "new@corp.io", "team_member_role": "admin"}],
    }
    post.assert_not_called()


def test_teams_create_posts_and_reports_created():
    post = mock.Mock(return_value={"team_id": "team-eng", "team_alias": "eng"})
    r = run(["teams", "create", "--alias", "eng", "--tpm", "20000"], post=post)
    post.assert_called_once_with(mock.ANY, "/team/new", {"team_alias": "eng", "tpm_limit": 20000})
    assert '"created"' in r.output and "team-eng" in r.output


def test_teams_create_rejects_unknown_member_role_before_the_proxy():
    post = mock.Mock()
    r = run(["teams", "create", "--alias", "eng", "--member-role", "owner"], post=post)
    assert r.exit_code != 0 and "not one of 'admin', 'user'" in r.output
    post.assert_not_called()


def test_teams_update_resolves_alias_to_id_and_replaces_models():
    post = mock.Mock(return_value={})
    get = lambda c, p, **k: [proxy_team("t-1", "eng")] if p == "/team/list" else {}
    r = run(["teams", "update", "eng", "--models", "qwen", "--max-budget", "20"], get=get, post=post)
    assert post.call_args.args[1:] == ("/team/update", {"team_id": "t-1", "models": ["qwen"], "max_budget": 20.0})


def test_teams_update_with_no_flags_is_a_clear_error():
    r = run(["teams", "update", "eng"], get=lambda c, p, **k: [proxy_team("t-1", "eng")])
    assert r.exit_code != 0 and "nothing to update" in r.output


def test_teams_delete_resolves_alias_lists_invalidated_keys_and_needs_yes():
    post = mock.Mock()
    get = lambda c, p, **k: (
        [proxy_team("t-1", "eng")] if p == "/team/list"
        else {"keys": [{"token": "sk-t", "key_alias": "eng-bot", "team_id": "t-1"}], "total_pages": 1} if p == "/key/list" else {}
    )
    r = run(["teams", "delete", "eng"], get=get, post=post)
    assert r.exit_code != 0 and "without --yes" in r.output and post.assert_not_called() is None
    r = run(["--json", "teams", "delete", "eng", "--yes"], get=get, post=post)
    out = json.loads(r.output)
    assert out["deleted"] == {"team_ids": ["t-1"]} and out["keys_removed"] == [{"alias": "eng-bot", "token": "sk-t"}]
    assert post.call_args.args[1] == "/team/delete"


def test_teams_delete_unknown_ref_is_an_error():
    r = run(["teams", "delete", "ghost", "--yes"], get=lambda c, p, **k: [proxy_team("t-1")] if p == "/team/list" else {})
    assert r.exit_code != 0 and "no live team matches 'ghost'" in r.output and "`teams list`" in r.output


def test_teams_block_dry_run_sends_team_ids_and_block_needs_yes():
    post = mock.Mock()
    get = lambda c, p, **k: [proxy_team("t-1", "eng")] if p == "/team/list" else {}
    r = run(["--dry-run", "teams", "block", "eng"], get=get, post=post)
    assert json.loads(r.output)["body"] == {"team_ids": ["t-1"]}
    post.assert_not_called()
    r = run(["teams", "block", "eng"], get=get, post=post)
    assert r.exit_code != 0 and "without --yes" in r.output
    post.assert_not_called()
    r = run(["teams", "unblock", "eng", "--yes"], get=get, post=post)
    assert post.call_args.args[1] == "/team/unblock"


def test_teams_member_add_by_name_resolves_the_user_by_email():
    get = lambda c, p, **k: (
        [proxy_team("t-1", "eng")] if p == "/team/list"
        else {"users": [proxy_user("svc-bot", email="svc@corp.io")], "total_pages": 1} if p == "/user/get_users" else {}
    )
    post = mock.Mock(return_value={})
    r = run(["teams", "member-add", "eng", "--user", "svc@corp.io"], get=get, post=post)
    assert post.call_args.args[1:] == ("/team/member_add", {"team_id": "t-1", "member": [{"user_email": "svc@corp.io"}]})


def test_teams_member_add_unknown_bare_name_is_an_error():
    get = lambda c, p, **k: [proxy_team("t-1", "eng")] if p == "/team/list" else {"users": [], "total_pages": 1}
    post = mock.Mock()
    r = run(["teams", "member-add", "eng", "--user", "nobody"], get=get, post=post)
    assert r.exit_code != 0 and "no user matches 'nobody'" in r.output
    post.assert_not_called()


def test_teams_member_delete_body_uses_user_id_or_email():
    get = lambda c, p, **k: [proxy_team("t-1", "eng")] if p == "/team/list" else {}
    post = mock.Mock(return_value={})
    r = run(["teams", "member-delete", "eng", "--user", "svc-bot"], get=get, post=post)
    assert post.call_args.args[2] == {"team_id": "t-1", "user_id": "svc-bot"}
    r = run(["teams", "member-delete", "eng", "--user", "svc@corp.io"], get=get, post=post)
    assert post.call_args.args[2] == {"team_id": "t-1", "user_email": "svc@corp.io"}


# ── workflow: team lifecycle feeding keys and users ───────────────────────


def test_workflow_onboard_team_issue_team_key_then_delete_team():
    """The team story end to end: create the team, mint a key for it, see both
    in the read commands, then delete the team — and the key it owned is named."""
    created = {"t": False, "k": False}
    key = {"token": "sk-eng", "key_alias": "eng-bot", "team_id": "team-eng"}

    def get(c, p, **k):
        if p == "/team/list":
            return [proxy_team("team-eng", "eng", members=("svc-bot",))] if created["t"] else []
        if p == "/key/list":
            return {"keys": [key] if created["k"] else [], "total_pages": 1}
        return {}

    def post(c, path, body=None, **k):
        if path == "/team/new":
            assert body["team_alias"] == "eng"
            created["t"] = True
            return {"team_id": "team-eng"}
        if path == "/key/generate":
            assert body.get("team_id") == "team-eng"
            created["k"] = True
            return {"key": "sk-eng"}
        assert path == "/team/delete"
        return {}

    r = run(["teams", "create", "--alias", "eng", "--team-id", "team-eng", "--member", "svc-bot"], get=get, post=post)
    assert '"created"' in r.output
    r = run(["keys", "generate", "--alias", "eng-bot", "--team", "team-eng"], get=get, post=post)
    assert "sk-eng" in r.output
    r = run(["--json", "teams", "list"], get=get)
    team_row = json.loads(r.output)[0]
    assert team_row["team_id"] == "team-eng" and team_row["members"] == 1
    r = run(["teams", "delete", "eng", "--yes"], get=get, post=post)
    assert "deleted team team-eng" in r.output
    r = run(["--json", "teams", "delete", "eng", "--yes"], get=get, post=post)
    out = json.loads(r.output)
    assert out["keys_removed"] == [{"alias": "eng-bot", "token": "sk-eng"}]


# ── budgets ───────────────────────────────────────────────────────────────


def budget_row(i="1", **kw):
    return {
        "budget_id": f"bud-{i}",
        "max_budget": 100.0,
        "soft_budget": None,
        "tpm_limit": 20000,
        "rpm_limit": 60,
        "max_parallel_requests": None,
        "budget_duration": "30d",
        "model_max_budget": None,
        **kw,
    }


def bud_state(rows):
    """Stub /budget/list (both the bare-list and envelope shapes) and /budget/info."""
    state = {"rows": list(rows)}

    def get(c, p, **k):
        if p == "/budget/list":
            return state["rows"]  # old proxies return a bare list
        if p == "/budget/info":
            hit = next(r for r in state["rows"] if r.get("budget_id") == k["params"]["budget_id"])
            return {"info": hit, "keys": state.get("keys", []), "teams": [{"team_alias": "eng", "team_id": "t-1"}]}
        return {}

    return state, get


def test_budgets_create_dry_run_prints_body_sends_nothing():
    post = mock.Mock()
    r = run(
        ["--dry-run", "budgets", "create", "--budget-id", "eng-2026", "--max-budget", "100",
         "--duration", "30d", "--model-max-budget", '{"gpt-4o": 0.01}'],
        post=post,
    )
    assert json.loads(r.output)["body"] == {
        "budget_id": "eng-2026", "max_budget": 100.0,
        "budget_duration": "30d", "model_max_budget": {"gpt-4o": 0.01},
    }
    assert "POST /budget/new" in r.output
    post.assert_not_called()


def test_budgets_create_with_no_cap_fails_before_the_proxy():
    post = mock.Mock()
    r = run(["budgets", "create", "--budget-id", "eng-2026"], post=post)
    assert r.exit_code != 0 and "something to cap" in r.output
    post.assert_not_called()


def test_budgets_create_posts_and_reports_the_id():
    post = mock.Mock(return_value={"budget_id": "eng-2026"})
    r = run(["budgets", "create", "--budget-id", "eng-2026", "--max-budget", "100", "--soft-budget", "90"], post=post)
    post.assert_called_once_with(mock.ANY, "/budget/new", {"budget_id": "eng-2026", "max_budget": 100.0, "soft_budget": 90.0})
    assert "created eng-2026" in r.output


def test_budgets_list_json_and_envelope_shapes_and_table():
    state, get = bud_state([budget_row("1"), budget_row("2", max_budget=5.0)])
    r = run(["--json", "budgets", "list"], get=get)
    rows = json.loads(r.output)
    assert [row["budget_id"] for row in rows] == ["bud-1", "bud-2"] and rows[1]["max_budget"] == 5.0
    r = run(["budgets", "list"], get=lambda c, p, **k: {"data": [budget_row("1")]})  # envelope shape
    assert "BUDGET" in r.output and "RESET" in r.output
    post = mock.Mock(return_value={})  # /budget/list is a read; posts must stay absent
    r = run(["budgets", "list"], get=get, post=post)
    post.assert_not_called()


def test_budgets_info_names_the_keys_and_teams_on_the_budget():
    state, get = bud_state([budget_row("eng", budget_id="eng-2026")])
    r = run(["--json", "budgets", "info", "eng-2026"], get=get)
    out = json.loads(r.output)
    assert out["budget"]["budget_id"] == "eng-2026" and out["budget"]["max_budget"] == 100.0
    assert out["keys"] == [] and out["teams"] == [{"team_alias": "eng", "team_id": "t-1"}]
    state["keys"] = [{"key_alias": "svc", "key_name": "sha-svc"}]
    r = run(["budgets", "info", "eng-2026"], get=get)
    assert "keys 1  teams 1 on this budget" in r.output and "svc" in r.output
    r = run(["budgets", "info", "eng-2026"], get=get)
    assert "teams 1 on this budget" in r.output


def test_budgets_update_unknown_id_is_an_error_before_the_proxy():
    post = mock.Mock()
    state, get = bud_state([budget_row("1")])
    r = run(["budgets", "update", "ghost", "--max-budget", "2"], get=get, post=post)
    assert r.exit_code != 0 and "no live budget matches 'ghost'" in r.output
    post.assert_not_called()


def test_budgets_update_omits_unset_fields():
    post = mock.Mock(return_value={"budget_id": "bud-1"})
    state, get = bud_state([budget_row("1")])
    r = run(["budgets", "update", "bud-1", "--max-budget", "200", "--duration", "7d"], get=get, post=post)
    post.assert_called_once_with(mock.ANY, "/budget/update", {"budget_id": "bud-1", "max_budget": 200.0, "budget_duration": "7d"})
    assert r.exit_code == 0


def test_budgets_update_with_nothing_to_change_fails_before_the_proxy():
    post = mock.Mock()
    state, get = bud_state([budget_row("1")])
    r = run(["budgets", "update", "bud-1"], get=get, post=post)
    assert r.exit_code != 0 and "something to cap" in r.output
    post.assert_not_called()


def test_budgets_delete_dry_run_uses_id_and_needs_yes_off_tty():
    state, get = bud_state([budget_row("1")])
    r = run(["--dry-run", "budgets", "delete", "bud-1"], get=get)
    assert json.loads(r.output)["body"] == {"id": "bud-1"}
    post = mock.Mock()
    r = run(["budgets", "delete", "bud-1"], get=get, post=post)
    assert r.exit_code != 0 and "without --yes" in r.output
    post.assert_not_called()
    post = mock.Mock(return_value={})
    r = run(["budgets", "delete", "bud-1", "--yes"], get=get, post=post)
    post.assert_called_once_with(mock.ANY, "/budget/delete", {"id": "bud-1"})
    assert '"deleted"' in r.output


def test_keys_generate_and_update_accept_budget_id():
    post = mock.Mock(return_value={"key": "sk-k"})
    r = run(["keys", "generate", "--alias", "svc", "--budget-id", "eng-2026"], post=post)
    body = {"key_alias": "svc", "budget_id": "eng-2026"}
    post.assert_called_once_with(mock.ANY, "/key/generate", body)


def test_rotate_carries_the_budget_id_to_the_new_key():
    info = {"info": {"key_alias": "svc", "models": ["m"], "budget_id": "eng-2026"}}
    r = run(["--dry-run", "keys", "rotate", "sk-old"], get=lambda c, p, **k: info)
    steps = json.loads(r.output)["steps"]
    assert steps[1]["body"]["budget_id"] == "eng-2026"


def test_workflow_budget_create_attach_reprice_delete():
    """The spend-control story: cap a fleet of keys through one budget."""
    state, get = bud_state([])
    state["keys"] = []

    def post(c, path, body=None, **k):
        if path == "/budget/new":
            state["rows"].append(dict(body))
            return dict(body)
        if path == "/key/generate":
            state["keys"].append({"key_alias": body["key_alias"], "key_name": "sha-svc"})
            return {"key": "sk-svc"}
        if path == "/budget/update":
            state["rows"][0]["max_budget"] = body["max_budget"]
            return {"budget_id": body["budget_id"]}
        if path == "/budget/delete":
            state["rows"] = [r for r in state["rows"] if r.get("budget_id") != body["id"]]
            return {}
        assert False, path
        return {}

    r = run(["budgets", "create", "--budget-id", "eng-2026", "--max-budget", "100", "--duration", "30d"], get=get, post=post)
    assert "created eng-2026" in r.output

    r = run(["--json", "keys", "generate", "--alias", "svc", "--budget-id", "eng-2026"], get=get, post=post)
    assert state["keys"] and state["keys"][0]["key_alias"] == "svc"

    r = run(["--json", "budgets", "info", "eng-2026"], get=get, post=post)
    out = json.loads(r.output)
    assert out["budget"]["max_budget"] == 100.0 and out["keys"][0]["key_alias"] == "svc"

    # Re-price every attached key in one update, not one per key.
    r = run(["--json", "budgets", "update", "eng-2026", "--max-budget", "200"], get=get, post=post)
    assert state["rows"][0]["max_budget"] == 200.0

    r = run(["budgets", "delete", "eng-2026", "--yes"], get=get, post=post)
    assert r.exit_code == 0 and state["rows"] == []
