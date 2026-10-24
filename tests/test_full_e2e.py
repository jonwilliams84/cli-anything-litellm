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




# ── customers ─────────────────────────────────────────────────────────────


def proxy_customer(i="1", **kw):
    return {
        "user_id": f"cust-{i}",
        "alias": f"cust-{i}",
        "spend": 0.91234,
        "max_budget": 20,
        "budget_id": None,
        "tpm_limit": 500,
        "rpm_limit": 10,
        "blocked": False,
        **kw,
    }


def cust_state(rows, envelope=False):
    """Stub /customer/list (both response shapes) and /customer/info."""
    state = {"rows": list(rows)}

    def get(c, p, **k):
        if p == "/customer/list":
            if envelope:
                return {"customers": state["rows"], "total_pages": 1, "page": 1}
            return state["rows"]  # older proxies return a bare list
        if p == "/customer/info":
            hit = next(
                (r for r in state["rows"] if r.get("user_id") == (k.get("params") or {}).get("user_id")), {}
            )
            return dict(hit)
        return {}

    return state, get


def test_customers_list_json_shapes_and_table():
    state, get = cust_state([proxy_customer(1), proxy_customer(2, max_budget=5.0)])
    r = run(["--json", "customers", "list"], get=get)
    rows = json.loads(r.output)
    assert [row["user_id"] for row in rows] == ["cust-1", "cust-2"] and rows[1]["max_budget"] == 5.0
    r = run(["customers", "list"], get=get)
    assert "USER_ID" in r.output and "BUDGET" in r.output
    post = mock.Mock(return_value={})  # a read; posts must stay absent
    r = run(["customers", "list"], get=get, post=post)
    post.assert_not_called()


def test_customers_list_tolerates_the_paginated_envelope():
    state, get = cust_state([proxy_customer(1)], envelope=True)
    r = run(["--json", "customers", "list"], get=get)
    assert [row["user_id"] for row in json.loads(r.output)] == ["cust-1"]


def test_customers_list_follows_pagination():
    def get(c, p, **k):
        if p == "/customer/list":
            if k["params"]["page"] == 1:
                return {"customers": [proxy_customer(1)], "total_pages": 2}
            return {"customers": [proxy_customer(2)], "total_pages": 2}
        return {}

    r = run(["--json", "customers", "list"], get=get)
    assert [row["user_id"] for row in json.loads(r.output)] == ["cust-1", "cust-2"]


def test_customers_list_filter_by_budget():
    state, get = cust_state([proxy_customer(1), proxy_customer(2, budget_id="eng-2026")])
    r = run(["--json", "customers", "list", "--budget", "eng-2026"], get=get)
    assert [row["user_id"] for row in json.loads(r.output)] == ["cust-2"]


def test_customers_info_resolves_an_alias_to_the_user_id():
    state, get = cust_state([proxy_customer(1, alias="acme")])
    r = run(["customers", "info", "acme"], get=get)
    assert "acme" in r.output and "cust-1" in r.output


def test_customers_create_dry_run_prints_body_sends_nothing():
    post = mock.Mock()
    r = run(
        ["--dry-run", "customers", "create", "--user-id", "cust-x", "--alias", "acme",
         "--max-budget", "20", "--budget-id", "eng-2026", "--rpm", "10"],
        post=post,
    )
    assert json.loads(r.output)["body"] == {
        "user_id": "cust-x", "alias": "acme", "max_budget": 20.0,
        "budget_id": "eng-2026", "rpm_limit": 10,
    }
    assert "POST /customer/new" in r.output
    post.assert_not_called()


def test_customers_create_needs_a_user_id_or_alias():
    post = mock.Mock()
    r = run(["customers", "create", "--max-budget", "20"], post=post)
    assert r.exit_code != 0 and "--user-id" in r.output and "--alias" in r.output
    post.assert_not_called()


def test_customers_create_posts_the_body():
    post = mock.Mock(return_value={"user_id": "cust-x"})
    r = run(["customers", "create", "--user-id", "cust-x", "--max-budget", "20"], post=post)
    post.assert_called_once_with(mock.ANY, "/customer/new", {"user_id": "cust-x", "max_budget": 20.0})
    assert "created cust-x" in r.output


def test_customers_update_unknown_ref_is_an_error_before_the_proxy():
    post = mock.Mock()
    state, get = cust_state([proxy_customer(1)])
    r = run(["customers", "update", "ghost", "--max-budget", "2"], get=get, post=post)
    assert r.exit_code != 0 and "no live customer matches 'ghost'" in r.output and "`customers list`" in r.output
    post.assert_not_called()


def test_customers_update_resolves_alias_and_omits_unset_fields():
    post = mock.Mock(return_value={"user_id": "cust-1"})
    state, get = cust_state([proxy_customer(1, alias="acme")])
    r = run(["customers", "update", "acme", "--max-budget", "30"], get=get, post=post)
    post.assert_called_once_with(mock.ANY, "/customer/update", {"user_id": "cust-1", "max_budget": 30.0})
    assert r.exit_code == 0
    post = mock.Mock()
    r = run(["customers", "update", "acme"], get=get, post=post)
    assert r.exit_code != 0 and "nothing to update" in r.output
    post.assert_not_called()


def test_customers_block_unblock_resolve_speak_plural_and_block_needs_yes():
    state, get = cust_state([proxy_customer(1, alias="acme")])
    r = run(["--dry-run", "customers", "block", "acme"], get=get)
    assert json.loads(r.output)["body"] == {"user_ids": ["cust-1"]}
    post = mock.Mock()
    r = run(["customers", "block", "acme"], get=get, post=post)
    assert r.exit_code != 0 and "without --yes" in r.output
    post.assert_not_called()
    post = mock.Mock(return_value={"blocked": ["cust-1"]})
    r = run(["customers", "unblock", "acme"], get=get, post=post)  # unblock is not destructive
    assert post.call_args.args[1:] == ("/customer/unblock", {"user_ids": ["cust-1"]})
    r = run(["customers", "block", "acme", "--yes"], get=get, post=post)
    post.assert_called_with(mock.ANY, "/customer/block", {"user_ids": ["cust-1"]})


def test_customers_delete_dry_run_uses_user_ids_and_gates_on_yes():
    state, get = cust_state([proxy_customer(1)])
    r = run(["--dry-run", "customers", "delete", "cust-1"], get=get)
    assert json.loads(r.output)["body"] == {"user_ids": ["cust-1"]}  # the family's plural outlier
    post = mock.Mock()
    r = run(["customers", "delete", "cust-1"], get=get, post=post)
    assert r.exit_code != 0 and "without --yes" in r.output
    post.assert_not_called()
    post = mock.Mock(return_value={})
    r = run(["customers", "delete", "cust-1", "--yes"], get=get, post=post)
    post.assert_called_once_with(mock.ANY, "/customer/delete", {"user_ids": ["cust-1"]})
    assert '"deleted"' in r.output


def test_workflow_budget_capped_customer_blocked_then_deleted():
    """The end-user story: a customer onboarded onto a shared budget, blocked
    when it is abused, unblocked, then removed."""
    state, get = cust_state([], envelope=True)

    def post(c, path, body=None, **k):
        if path == "/customer/new":
            state["rows"].append(dict(body, spend=0, blocked=False))
            return dict(body)
        if path in ("/customer/block", "/customer/unblock"):
            for r_ in state["rows"]:
                if r_.get("user_id") in body["user_ids"]:
                    r_["blocked"] = path == "/customer/block"
            return {"blocked": body["user_ids"]}
        assert path == "/customer/delete", path
        state["rows"] = [r_ for r_ in state["rows"] if r_.get("user_id") not in body["user_ids"]]
        return {}

    r = run(["customers", "create", "--user-id", "cust-x", "--alias", "acme", "--budget-id", "eng-2026"], get=get, post=post)
    assert "created cust-x" in r.output

    r = run(["--json", "customers", "list", "--budget", "eng-2026"], get=get)
    rows = json.loads(r.output)
    assert [row["user_id"] for row in rows] == ["cust-x"] and rows[0]["budget_id"] == "eng-2026"

    r = run(["customers", "block", "acme", "--yes"], get=get, post=post)
    assert r.exit_code == 0
    r = run(["--json", "customers", "info", "acme"], get=get)
    assert json.loads(r.output)["blocked"] is True
    r = run(["customers", "unblock", "acme"], get=get, post=post)
    assert r.exit_code == 0
    r = run(["--json", "customers", "info", "acme"], get=get)
    assert json.loads(r.output)["blocked"] is False

    r = run(["customers", "delete", "cust-x", "--yes"], get=get, post=post)
    assert r.exit_code == 0 and state["rows"] == []


# ── logs list / logs summary ──────────────────────────────────────────────

import hashlib

SVC_TOKEN, OTHER_TOKEN = "b" * 64, "c" * 64
MASTER_HASH = hashlib.sha256(b"sk-master").hexdigest()


def log_row(i, **kw):
    """A /spend/logs row, as the proxy writes it (sha256 key, ISO timestamps)."""
    row = {
        "request_id": f"req-{i}",
        "call_type": "/chat/completion",
        "model": "bedrock/qwen-2.5",
        "model_group": "qwen",
        "api_key": SVC_TOKEN,
        "user": "svc-bot",
        "team_id": "eng",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "spend": 0.0005,
        "startTime": f"2026-10-11T00:0{i}:00",
        "endTime": f"2026-10-11T00:0{i}:02",
        "cache_hit": False,
        "metadata": {"user_api_key_alias": "svc"},
    }
    row.update(kw)
    return row


def log_proxy(rows):
    def get(c, p, **k):
        if p == "/spend/logs":
            return rows
        if p == "/key/list":
            return {"keys": [{"key_alias": "svc", "token": SVC_TOKEN}, {"key_alias": "web", "token": OTHER_TOKEN}], "total_pages": 1}
        return {}

    return get


def test_logs_list_rows_newest_first_with_named_caller():
    r = run(["--json", "logs", "list", "--days", "2"], get=log_proxy([log_row(2), log_row(1)]))
    d = json.loads(r.output)
    assert d["matched"] == 2 and d["window_days"] == 2
    assert [row["request_id"] for row in d["rows"]] == ["req-2", "req-1"]
    assert d["rows"][0]["caller"] == "svc" and SVC_TOKEN not in r.output
    assert d["rows"][0]["spend"] == 0.0005 and d["rows"][0]["latency_ms"] == 2000.0


def test_logs_list_names_the_master_key_and_later_gone_keys():
    r = run(
        ["--json", "logs", "list"],
        get=log_proxy([log_row(1, api_key=MASTER_HASH), log_row(2, api_key="f" * 64, metadata={"user_api_key_alias": "old-svc"})]),
    )
    callers = {row["caller"] for row in json.loads(r.output)["rows"]}
    assert callers == {"(master key)", "old-svc (gone)"}


def test_logs_list_filters_by_alias_user_team_model():
    rows = [
        log_row(1),
        log_row(2, user="web-end", model_group="embed", model="embed", api_key=OTHER_TOKEN, team_id=None),
        log_row(3, model_group="embed", model="embed", api_key=OTHER_TOKEN, team_id="ops"),
    ]

    def get(c, p, **k):
        if p == "/spend/logs":
            return rows
        if p == "/key/list":
            return {"keys": [{"key_alias": "svc", "token": SVC_TOKEN}, {"key_alias": "web", "token": OTHER_TOKEN}], "total_pages": 1}
        return {}

    d = json.loads(run(["--json", "logs", "list", "--key", "svc"], get=get).output)
    assert [row["request_id"] for row in d["rows"]] == ["req-1"] and d["filters"]["key"] == "svc"
    assert json.loads(run(["--json", "logs", "list", "--user", "web-end"], get=get).output)["matched"] == 1
    assert json.loads(run(["--json", "logs", "list", "--team", "ops"], get=get).output)["matched"] == 1
    d = json.loads(run(["--json", "logs", "list", "--model", "embed", "--limit", "1"], get=get).output)
    assert d["matched"] == 2 and len(d["rows"]) == 1 and d["rows"][0]["request_id"] == "req-3"


def test_logs_list_refuses_ambiguous_alias_before_the_proxy():
    saw_logs = []

    def get(c, p, **k):
        if p == "/spend/logs":
            saw_logs.append(True)
            return [log_row(1)]
        if p == "/key/list":
            return {"keys": [{"key_alias": "svc", "token": SVC_TOKEN}, {"key_alias": "svc", "token": OTHER_TOKEN}], "total_pages": 1}
        return {}

    r = run(["--json", "logs", "list", "--key", "svc"], get=get)
    assert r.exit_code != 0 and "matches 2 keys" in r.output and not saw_logs


def test_logs_summary_totals_and_human_output():
    rows = [
        log_row(1, spend=0.5),
        log_row(2, spend=0.5, completion_tokens=0),
        log_row(3, spend=0.2, model_group="embed", model="embed", startTime="2026-10-11T05:00:00", endTime="2026-10-11T05:00:04"),
        log_row(4, spend=0.0, cache_hit=True),
    ]
    r = run(["--json", "logs", "summary"], get=log_proxy(rows))
    s = json.loads(r.output)
    assert s["requests"] == 4 and s["spend"] == 1.2 and s["cache_hits"] == 1
    assert s["latency_ms"]["p50"] == 2000.0 and s["latency_ms"]["p95"] == 4000.0 and s["latency_ms"]["max"] == 4000.0
    assert s["models"][0]["model"] == "qwen" and s["models"][0]["latency_ms"]["p50"] == 2000.0
    assert s["models"][1]["model"] == "embed" and s["models"][1]["latency_ms"]["p50"] == 4000.0

    r = run(["logs", "summary"], get=log_proxy(rows))
    assert "4 request(s) in the last 7 day(s): $1.2 · cache 1 hit(s)" in r.output
    assert "p50/p95/max: 2000.0ms / 4000.0ms / 4000.0ms" in r.output


def test_workflow_spend_total_then_logs_drill_down_per_key():
    """`spend --by key` shows a key burning budget; `logs list --key` names the
    requests behind that number — same log, drill-down view."""
    rows = [log_row(1, spend=0.5), log_row(2, spend=0.5), log_row(3, spend=9.0, api_key=OTHER_TOKEN)]

    r = run(["--json", "spend", "--by", "key"], get=log_proxy(rows))
    per_key = {row["key"]: row["spend"] for row in json.loads(r.output)["rows"]}
    assert per_key["svc"] == 1.0 and per_key["web"] == 9.0

    d = json.loads(run(["--json", "logs", "list", "--key", "svc"], get=log_proxy(rows)).output)
    assert d["matched"] == 2
    assert {row["request_id"] for row in d["rows"]} == {"req-1", "req-2"}


# ── tags: key tagging and per-tag spend attribution ───────────────────────


def test_keys_generate_tags_dry_run_sends_metadata_sends_nothing():
    post = mock.Mock()
    r = run(["--dry-run", "keys", "generate", "--alias", "feat", "--tags", "feat-x, team-a,feat-x"], post=post)
    assert json.loads(r.output)["body"]["metadata"] == {"tags": ["feat-x", "team-a"]}
    post.assert_not_called()


def test_keys_generate_refuses_a_tag_of_nothing_before_the_proxy():
    post = mock.Mock()
    r = run(["keys", "generate", "--alias", "feat", "--tags", " , ,"], post=post)
    assert r.exit_code != 0 and "no usable tag names" in r.output
    post.assert_not_called()


def test_keys_update_tags_merge_over_existing_metadata():
    keys = {"keys": [{"token": "tok-1", "key_alias": "svc", "metadata": {"team": "eng", "tags": ["old"]}}], "total_pages": 1}
    post = mock.Mock(return_value={"token": "tok-1"})
    r = run(["--json", "keys", "update", "svc", "--tags", "feat-x,old"], get=lambda c, p, **k: keys, post=post)
    body = post.call_args.args[2]
    assert body["key"] == "tok-1"
    assert body["metadata"] == {"team": "eng", "tags": ["feat-x", "old"]}  # tags replaced, rest kept


def test_keys_update_refuses_empty_tags_before_the_proxy():
    post = mock.Mock()
    r = run(["keys", "update", "svc", "--tags", " ,"], post=post)
    assert r.exit_code != 0 and "no usable tag names" in r.output
    post.assert_not_called()


def test_keys_list_rows_carry_tags_from_both_shapes():
    keys = {
        "keys": [
            {"key_alias": "svc", "metadata": {"tags": ["b", "a"]}},
            {"key_alias": "old", "tags": ["z"]},  # older proxies flat-map tags
            {"key_alias": "plain"},
        ],
        "total_pages": 1,
    }
    r = run(["--json", "keys", "list"], get=lambda c, p, **k: keys)
    rows = json.loads(r.output)
    assert [row["tags"] for row in rows] == ["a, b", "z", None]


TAGS = ["feat-x", "team-a"]


def test_spend_by_tag_counts_each_tag_and_untagged_once():
    rows = [
        log_row(1, spend=1.0, request_tags=json.dumps(TAGS)),
        log_row(2, spend=0.5, request_tags=["feat-x"]),
        log_row(3, spend=0.25, request_tags=json.dumps(["team-a"])),
        log_row(4, spend=0.25),  # no tags
    ]
    r = run(["--json", "spend", "--by", "tag"], get=log_proxy(rows))
    d = json.loads(r.output)
    assert d["group_by"] == "tag" and d["log_rows"] == 4
    assert [row["tag"] for row in d["rows"]] == ["feat-x", "team-a", "(untagged)"]
    feat = next(row for row in d["rows"] if row["tag"] == "feat-x")
    assert feat["requests"] == 2 and feat["spend"] == 1.5 and feat["prompt_tokens"] == 200

    r = run(["spend", "--by", "tag"], get=log_proxy(rows))
    assert "feat-x" in r.output and "(untagged)" in r.output


def test_logs_list_and_summary_filter_by_tag():
    rows = [
        log_row(1, request_tags='["feat-x","team-a"]'),
        log_row(2, request_tags=json.dumps(["team-a"])),
        log_row(3),
    ]
    d = json.loads(run(["--json", "logs", "list", "--tag", "feat-x"], get=log_proxy(rows)).output)
    assert d["filters"] == {"tag": "feat-x"} and d["matched"] == 1 and d["rows"][0]["request_id"] == "req-1"
    assert d["rows"][0]["tags"] == ["feat-x", "team-a"]

    s = json.loads(run(["--json", "logs", "summary", "--tag", "team-a"], get=log_proxy(rows)).output)
    assert s["requests"] == 2 and s["filters"] == {"tag": "team-a"}


def test_workflow_tag_chargeback_generate_then_spend_then_logs_drill_down():
    """The point of tags: mint `feat-x` onto a key, then bill the feature from
    the log — `spend --by tag` for the money, `logs list --tag` for the rows,
    `keys update --tags` to move later traffic onto the right label."""
    post = mock.Mock(return_value={"token": "sk-new"})
    r = run(["--json", "keys", "generate", "--alias", "feat", "--tags", "feat-x"], post=post)
    assert post.call_args.args[1:] == ("/key/generate", {"key_alias": "feat", "metadata": {"tags": ["feat-x"]}})

    rows = [
        log_row(1, request_tags='["feat-x"]', spend=2.0),
        log_row(2, request_tags=json.dumps(["other"]), spend=1.0),
    ]

    def get(c, p, **k):
        if p == "/spend/logs":
            return rows
        if p == "/key/list":
            return {"keys": [{"key_alias": "feat", "token": SVC_TOKEN, "metadata": {"tags": ["feat-x"]}}], "total_pages": 1}
        return {}

    per_tag = json.loads(run(["--json", "spend", "--by", "tag"], get=get).output)["rows"]
    assert [row["tag"] for row in per_tag] == ["feat-x", "other"]

    d = json.loads(run(["--json", "logs", "list", "--tag", "feat-x", "--limit", "1"], get=get).output)
    assert d["matched"] == 1 and d["rows"][0]["caller"] == "feat" and d["rows"][0]["tags"] == ["feat-x"]

    # a retag sends the merged metadata; the old tags stop accruing from now on
    post2 = mock.Mock(return_value={"token": SVC_TOKEN})
    r = run(["--dry-run", "keys", "update", "feat", "--tags", "feat-y, feat-x"], get=get, post=post2)
    assert json.loads(r.output)["body"]["metadata"] == {"tags": ["feat-y", "feat-x"]}


# ── usage report (server-side spend aggregation) ──────────────────────────

HASH = "a" * 64
REPORT_ROWS = [
    {"date": "2026-10-02", "api_key": HASH, "spend": 0.25, "prompt_tokens": 40, "completion_tokens": 12},
    {"date": "2026-10-02", "team_id": "team-q", "spend": 1.0},
    {"date": "2026-10-01", "api_key": HASH, "spend": 1.5, "completion_tokens": 30},
    {"data": "not a row"},  # tolerate junk in the envelope rather than crash
]


def test_usage_report_day_totals_group_client_side():
    def get(c, p, **k):
        assert p == "/global/spend/report"
        return REPORT_ROWS

    r = run(["usage", "report", "--days", "7"], get=get)
    assert r.exit_code == 0
    assert "by day: $2.75 over 2 day(s)" in r.output  # 1.5 + 0.25 + 1.0


def test_usage_report_days_window_is_the_query_it_sends():
    seen = {}

    def get(c, p, **k):
        if p == "/global/spend/report":
            seen.update(k.get("params") or {})
        return []

    run(["usage", "report", "--days", "3"], get=get)
    assert "group_by" not in seen  # day has no server-side form: default rows, summed client-side
    from datetime import date, timedelta

    e = date.today() + timedelta(days=1)
    assert seen["start_date"] == (e - timedelta(days=4)).isoformat() and seen["end_date"] == e.isoformat()


def test_usage_report_explicit_range_overrides_days_and_lands_in_the_query():
    seen = {}

    def get(c, p, **k):
        if p == "/global/spend/report":
            seen.update(k.get("params") or {})
        return []

    r = run(["usage", "report", "--days", "3", "--start", "2026-01-01", "--end", "2026-01-31"], get=get)
    assert r.exit_code == 0 and seen == {"start_date": "2026-01-01", "end_date": "2026-01-31"}


def test_usage_report_range_errors_before_the_proxy():
    post = mock.Mock()
    for args in (
        ["usage", "report", "--start", "2026-01-01"],
        ["usage", "report", "--start", "2026-1-1", "--end", "2026-01-31"],
        ["usage", "report", "--start", "2026-02-01", "--end", "2026-01-31"],
    ):
        r = run(args, post=post)
        assert r.exit_code != 0 and "Error" in r.output
    post.assert_not_called()


def test_usage_report_api_key_rows_are_named_like_spend_by_key():
    def get(c, p, **k):
        if p == "/global/spend/report":
            return {"data": REPORT_ROWS}
        if p == "/key/list":
            return {"keys": [{"token": HASH, "key_alias": "svc"}]}
        return {}

    r = run(["--json", "usage", "report", "--by", "api_key", "--days", "7"], get=get)
    assert r.exit_code == 0
    out = json.loads(r.output)
    names = {row["api_key"] for row in out["rows"]}
    assert "svc" in names  # the hash is named through the live key list
    assert HASH not in names  # and the secret-shaped hash never leaks into the output
    first = next(row for row in out["rows"] if row["date"] == "2026-10-02" and row["api_key"] == "svc")
    assert first["spend"] == 0.25 and first["prompt_tokens"] == 40
    assert out["rows"][-1]["api_key"] == "(unattributed)"  # the junk row did not crash the view


def test_usage_report_unknown_hash_is_unknown_not_a_secret():
    def get(c, p, **k):
        if p == "/global/spend/report":
            return {"data": [{"date": "2026-10-01", "api_key": "f" * 64, "spend": 1.0}]}
        return {}

    r = run(["usage", "report", "--by", "api_key", "--days", "7"], get=get)
    assert "svc" not in r.output and HASH not in r.output and "(unknown ffffffffff...)" in r.output


def test_usage_report_entity_sanity_and_limit():
    r = run(["--json", "usage", "report", "--by", "team", "--days", "7", "--limit", "1"], get=lambda c, p, **k: REPORT_ROWS if p == "/global/spend/report" else {})
    assert r.exit_code == 0
    out = json.loads(r.output)
    assert len(out["rows"]) == 1
    assert out["window"] == {"start_date": out["window"]["start_date"], "end_date": out["window"]["end_date"]}
    row = out["rows"][0]
    assert row["date"] == "2026-10-02" and row["team"] == "team-q" and row["spend"] == 1.0


# ── workflow: the wide-window money view matches the drill-down window ────


def test_workflow_spend_by_key_and_usage_report_agree_and_logs_drill_down():
    """`spend --by key` (raw log, client-side) and `usage report --by api_key`
    (server-side report) name the same caller the same way; `logs list --key`
    then shows the requests behind it."""
    log_rows = [
        {
            "api_key": HASH,
            "spend": 1.5,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "startTime": "2026-10-01T10:00:00",
            "endTime": "2026-10-01T10:00:03",
            "model": "qwen",
            "request_tags": '["feat-x"]',
        },
        {
            "api_key": HASH,
            "spend": 0.25,
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "startTime": "2026-10-02T10:00:00",
            "endTime": "2026-10-02T10:00:02",
            "model": "qwen",
        },
    ]

    # the proxy's own report sees the same log: the two requests, per key and day
    report_rows = [
        {"date": "2026-10-01", "api_key": HASH, "spend": 1.5, "prompt_tokens": 10, "completion_tokens": 5},
        {"date": "2026-10-02", "api_key": HASH, "spend": 0.25, "prompt_tokens": 2, "completion_tokens": 1},
    ]

    def get(c, p, **k):
        if p == "/key/list":
            return {"keys": [{"token": HASH, "key_alias": "svc"}]}
        if p == "/spend/logs":
            return log_rows
        if p == "/global/spend/report":
            return {"data": report_rows}
        return {}

    r1 = run(["spend", "--by", "key", "--days", "7"], get=get)
    assert r1.exit_code == 0 and "svc" in r1.output and "1.75" in r1.output
    r2 = run(["--json", "usage", "report", "--by", "api_key", "--days", "7"], get=get)
    out = json.loads(r2.output)
    assert r2.exit_code == 0 and round(out["spend"], 2) == 1.75 and "svc" in json.dumps(out["rows"])
    r3 = run(["logs", "list", "--key", "svc", "--days", "7"], get=get)
    assert r3.exit_code == 0 and r3.output.count("qwen") == 2
