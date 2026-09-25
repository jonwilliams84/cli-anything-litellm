"""Offline tests: every behaviour here was first observed against a real
LiteLLM v1.100.1 proxy; the fixtures reproduce the shapes it returned."""

from __future__ import annotations

import hashlib
import json
from unittest import mock

import pytest
from click.testing import CliRunner

from cli_anything.litellm import litellm_cli as cli_mod
from cli_anything.litellm.core import backend as be
from cli_anything.litellm.core import drift, lint
from cli_anything.litellm.core import models as m


def dep(i, name="qwen", base=None, ctx=131072, budget=4096, mode="chat", db=False, key="os.environ/K"):
    return {
        "model_name": name,
        "litellm_params": {
            "model": f"hosted_vllm/{name}",
            "api_base": base or f"http://n{i}:8000/v1",
            "api_key": key,
            "extra_body": {
                "chat_template_kwargs": {"reasoning_effort": "low"},
                "thinking_token_budget": budget,
            },
        },
        "model_info": {
            "id": f"node-{i}",
            "mode": mode,
            "max_input_tokens": ctx,
            "input_cost_per_token": 3e-06,
            "db_model": db,
        },
    }


def live(d):
    """What /model/info does to a config deployment: masks *token* keys, drops env refs."""
    d = json.loads(json.dumps(d))
    d["litellm_params"].pop("api_key", None)
    d["litellm_params"]["extra_body"]["thinking_token_budget"] = "****"
    d["litellm_params"]["use_in_pass_through"] = False
    d["model_info"]["supports_vision"] = True  # a default config never set
    return d


# ── models ────────────────────────────────────────────────────────────────


def test_summarize_groups_replicas_and_counts_db_models():
    rows = m.summarize([dep(1), dep(2), dep(3, db=True), dep(4, name="embed", mode="embedding")])
    q = next(r for r in rows if r["model_name"] == "qwen")
    assert q["replicas"] == 3 and q["db_replicas"] == 1 and len(q["backends"]) == 3


def test_replica_inconsistency_names_the_odd_one():
    issues = m.replica_inconsistencies([dep(1), dep(2), dep(3, ctx=262144)])
    assert issues and issues[0]["odd_ones"] == [["node-3"]]


def test_backends_alone_are_not_inconsistent():
    assert m.replica_inconsistencies([dep(1), dep(2)]) == []


# ── drift ─────────────────────────────────────────────────────────────────


def test_in_sync_despite_masking_env_refs_and_defaults():
    cfg = [dep(1), dep(2)]
    res = drift.compare_deployments(cfg, [live(d) for d in cfg])
    assert res["changed"] == [] and res["only_live"] == []
    assert res["unverifiable_masked_params"] == ["extra_body.thinking_token_budget"]


def test_real_change_is_reported_with_both_values():
    res = drift.compare_deployments([dep(1)], [live(dep(1, base="http://elsewhere:8000/v1"))])
    assert res["changed"][0]["diffs"]["params.api_base"] == {
        "config": "http://n1:8000/v1",
        "live": "http://elsewhere:8000/v1",
    }


def test_db_only_deployment_is_flagged():
    res = drift.compare_deployments([dep(1)], [live(dep(1)), live(dep(9, db=True))])
    assert res["only_live"] == [{"id": "node-9", "model_name": "qwen", "db_model": True}]


def test_router_int_float_is_not_drift_but_strategy_is():
    got = drift.compare_router(
        {"num_retries": 2, "routing_strategy": "least-busy", "redis_host": "os.environ/X"},
        {"num_retries": 2.0, "routing_strategy": "simple-shuffle"},
    )
    assert got == [{"key": "routing_strategy", "config": "least-busy", "live": "simple-shuffle"}]


def test_guardrail_mode_order_is_not_drift():
    a = [{"guardrail_name": "g", "litellm_params": {"mode": ["pre_call", "post_call"], "default_on": True}}]
    b = [{"guardrail_name": "g", "litellm_params": {"mode": ["post_call", "pre_call"], "default_on": True}}]
    assert drift.compare_guardrails(a, b)["changed"] == []


def test_fleet_diff_labels_and_error_node():
    snap = {
        "version": "1.100.1",
        "deployments": [live(dep(1))],
        "router": {"routing_strategy": "least-busy"},
        "guardrails": [],
        "keys": 3,
    }
    other = dict(snap, router={"routing_strategy": "simple-shuffle"})
    res = drift.fleet_diff({"a": snap, "b": other, "c": {"error": "401 ... master keys differ"}})
    assert res["identical"] is False and res["reference"] == "a"
    b = res["proxies"][1]
    assert b["router"] == [{"key": "routing_strategy", "reference": "least-busy", "node": "simple-shuffle"}]
    assert "only_on_reference" in b["deployments"] and "error" in res["proxies"][2]


def test_fleet_identical():
    snap = {"version": "1", "deployments": [live(dep(1))], "router": {}, "guardrails": [], "keys": 1}
    assert drift.fleet_diff({"a": snap, "b": json.loads(json.dumps(snap))})["identical"] is True


# ── lint ──────────────────────────────────────────────────────────────────


def rules(res):
    return sorted({f["rule"] for f in res["findings"]})


def test_placeholder_key_is_info_literal_key_is_error():
    cfg = {"model_list": [dep(1, key="none"), dep(2, key="sk-realkey123456789")]}
    res = lint.lint(cfg)
    lv = {f["rule"]: f["level"] for f in res["findings"]}
    assert lv["placeholder-credential"] == "info" and lv["literal-secret"] == "error"


def test_duplicate_and_missing_ids():
    a, b, c = dep(1), dep(1), dep(3)
    del c["model_info"]["id"]
    assert {"duplicate-id", "no-deployment-id"} <= set(rules(lint.lint({"model_list": [a, b, c]})))


POLICY = {
    "rules": [
        {"id": "lb", "path": "router_settings.routing_strategy", "equals": "least-busy"},
        {"id": "aff", "path": "router_settings.optional_pre_call_checks", "contains": "session_affinity"},
        {"id": "bridge", "path": "guardrails", "contains": {"guardrail_name": "bridge"}},
        {
            "id": "ctx",
            "each_deployment": {"mode": "chat"},
            "path": "model_info.max_input_tokens",
            "equals": 131072,
        },
        {"id": "mk", "path": "general_settings.master_key", "startswith": "os.environ/"},
    ]
}


def test_policy_passes_good_config():
    cfg = {
        "model_list": [dep(1), dep(2, mode="embedding", ctx=8192)],
        "router_settings": {
            "routing_strategy": "least-busy",
            "optional_pre_call_checks": ["session_affinity"],
        },
        "guardrails": [{"guardrail_name": "bridge"}],
        "general_settings": {"master_key": "os.environ/MK"},
    }
    assert lint.apply_policy(cfg, POLICY) == []


def test_policy_catches_the_2026_09_24_drift_and_masks_secrets():
    cfg = {
        "model_list": [dep(1), dep(2, ctx=262144)],
        "router_settings": {"routing_strategy": "simple-shuffle"},
        "general_settings": {"master_key": "sk-leakedkey1234567"},
    }
    found = lint.apply_policy(cfg, POLICY)
    assert sorted({f["rule"] for f in found}) == ["aff", "bridge", "ctx", "lb", "mk"]
    assert [f["where"] for f in found if f["rule"] == "ctx"] == ["node-2"]
    assert "leakedkey" not in json.dumps(found)


def test_bad_rule_is_reported_not_crashed():
    assert lint.apply_policy({}, {"rules": [{"id": "x", "path": "a"}]})[0]["level"] == "error"


# ── backend ───────────────────────────────────────────────────────────────


def test_resolve_conn_precedence(monkeypatch, tmp_path):
    monkeypatch.setattr(be, "CONFIG_PATH", tmp_path / "c.json")
    be.save({"url": "http://saved", "key": "sk-saved"})
    monkeypatch.setenv("LITELLM_URL", "http://env/")
    c = be.resolve_conn(None, "sk-flag")
    assert c["url"] == "http://env" and c["key"] == "sk-flag"
    assert oct((tmp_path / "c.json").stat().st_mode)[-3:] == "600"


def test_request_without_url_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(be, "CONFIG_PATH", tmp_path / "none.json")
    monkeypatch.delenv("LITELLM_URL", raising=False)
    with pytest.raises(be.ApiError, match="no proxy URL"):
        be.get(be.resolve_conn(), "/x")


# ── CLI ───────────────────────────────────────────────────────────────────


def run(args, get=None, post=None, stdin=None):
    with (
        mock.patch.object(be, "get", side_effect=get or (lambda *a, **k: {})),
        mock.patch.object(be, "post", side_effect=post or (lambda *a, **k: {})),
    ):
        return CliRunner().invoke(
            cli_mod.cli,
            ["--url", "http://p", "--key", "sk-master", *args],
            input=stdin,
            catch_exceptions=False,
        )


def test_keys_generate_dry_run_sends_nothing():
    post = mock.Mock()
    r = run(["--dry-run", "keys", "generate", "--alias", "a", "--models", "x,y", "--rpm", "5"], post=post)
    assert json.loads(r.output)["body"] == {"key_alias": "a", "models": ["x", "y"], "rpm_limit": 5}
    post.assert_not_called()


def test_delete_refuses_without_yes_off_tty():
    post = mock.Mock()
    r = run(["keys", "delete", "sk-abc"], post=post)
    assert r.exit_code != 0 and "without --yes" in r.output
    post.assert_not_called()


def test_rotate_dry_run_plans_three_steps_and_carries_limits():
    info = {"info": {"key_alias": "svc", "models": ["m"], "rpm_limit": 10, "max_budget": 2.0, "spend": 1.5}}
    r = run(["--dry-run", "keys", "rotate", "sk-old"], get=lambda c, p, **k: info)
    steps = json.loads(r.output)["steps"]
    assert [s["call"] for s in steps] == ["POST /key/update", "POST /key/generate", "POST /key/delete"]
    assert steps[1]["body"] == {"key_alias": "svc", "models": ["m"], "rpm_limit": 10, "max_budget": 2.0}


def test_spend_by_key_names_master_and_deleted_keys():
    master = hashlib.sha256(b"sk-master").hexdigest()
    logs = [
        {"api_key": master, "spend": 0, "model_group": ""},
        {
            "api_key": "a" * 64,
            "spend": 0.5,
            "model_group": "q",
            "metadata": {"user_api_key_alias": "old-svc"},
            "prompt_tokens": 10,
        },
    ]

    def get(c, p, **k):
        return logs if p == "/spend/logs" else {"keys": [], "total_pages": 1}

    r = run(["--json", "spend", "--by", "key"], get=get)
    names = {row["key"] for row in json.loads(r.output)["rows"]}
    assert names == {"(master key)", "old-svc (gone)"}


def test_401_hint_mentions_master_key_mismatch():
    def get(c, p, **k):
        raise be.ApiError(401, "Authentication Error", "http://p" + p)

    with mock.patch.object(be, "get", side_effect=get):
        r = CliRunner().invoke(cli_mod.cli, ["--url", "http://p", "--key", "k", "models", "list"])
    assert r.exit_code != 0 and "master keys differ" in r.output
