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
