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
