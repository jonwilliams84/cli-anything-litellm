"""Routing resilience (v0.12.0): fallbacks, paused deployments, recorded health.

Core units, the CLI over a mocked proxy, drift/fleet over fallback chains,
two workflows and the installed command. Fixtures (tests/resilience_fixtures.py)
reproduce LiteLLM v1.106.3 response shapes; each cites the function it mirrors.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
from click.testing import CliRunner

from cli_anything.litellm import litellm_cli as cli_mod
from cli_anything.litellm.core import backend as be
from cli_anything.litellm.core import drift
from cli_anything.litellm.core import fallbacks as fb
from cli_anything.litellm.core import health as hc
from cli_anything.litellm.core import models as m

from resilience_fixtures import (
    COMPLETION,
    DEPLOYMENTS,
    HISTORY_ROWS,
    LATEST_ROWS,
    UPSTREAM_ERROR,
    blocked_row,
    completion_headers,
    deployment,
    fallback_created,
    fallback_deleted,
    fallback_get,
    health_row,
    history,
    latest,
    no_fallback_text,
    router_settings,
)

SECRETS = ("SECRET", "sk-proj", "ENCRYPTED")


def no_secret(text: str) -> None:
    for s in SECRETS:
        assert s not in text, s


# ── core/fallbacks.py ─────────────────────────────────────────────────────


def test_rules_every_stored_shape():
    assert fb.rules([{"qwen": ["gpt-4o", "claude"]}, {"*": ["small"]}]) == {
        "qwen": ["gpt-4o", "claude"],
        "*": ["small"],
    }
    assert fb.rules('[{"qwen": ["gpt-4o"]}]') == {"qwen": ["gpt-4o"]}  # JSON text from the DB row
    assert fb.rules(["small"]) == {fb.BARE: ["small"]}  # bare string: a chain for every group
    assert fb.rules([{"qwen": "gpt-4o"}]) == {"qwen": ["gpt-4o"]}
    assert fb.rules([{"qwen": ["a"]}, {"qwen": ["b"]}]) == {"qwen": ["b"]}
    assert fb.rules(None) == {} and fb.rules("not json") == {} and fb.rules({"x": 1}) == {}


def test_list_rows_types_sources_and_model_filter():
    cur = router_settings(
        {
            "fallbacks": [{"qwen": ["gpt-4o"]}, {"*": ["small"]}, {"claude": ["gpt-4o"]}],
            "context_window_fallbacks": [{"qwen": ["claude"]}],
        },
        {"fallbacks": "db", "context_window_fallbacks": "config"},
    )
    rows = fb.list_rows(cur["current_values"], cur["source"])
    assert {(r["model"], r["type"], r["source"]) for r in rows} == {
        ("qwen", "general", "db"),
        ("*", "general", "db"),
        ("claude", "general", "db"),
        ("qwen", "context_window", "config"),
    }
    only = fb.list_rows(cur["current_values"], cur["source"], model="qwen", fallback_type="general")
    assert [r["model"] for r in only] == ["qwen", "*"]  # its own rule plus the catch-all


def test_config_owned():
    assert fb.config_owned({"fallbacks": "config"}, "general") is True
    assert fb.config_owned({"fallbacks": "db"}, "general") is False
    assert fb.config_owned({"fallbacks": "config"}, "context_window") is False
    assert fb.config_owned(None, "general") is False


def test_new_body_mirrors_fallback_create_request():
    known = {"qwen", "gpt-4o", "claude"}
    assert fb.new_body(" qwen ", ["gpt-4o", " claude"], "general", known) == {
        "model": "qwen",
        "fallback_models": ["gpt-4o", "claude"],
        "fallback_type": "general",
    }
    assert fb.new_body("*", ["gpt-4o"], "general", known)["model"] == "*"  # the catch-all key is valid


@pytest.mark.parametrize(
    ("model", "to", "ftype", "msg"),
    [
        ("", ["a"], "general", "model group is required"),
        ("qwen", [" ", ""], "general", "at least one"),
        ("qwen", ["gpt-4o", "gpt-4o"], "general", "more than once"),
        ("qwen", ["qwen"], "general", "its own fallback"),
        ("qwen", ["nope"], "general", "not a live model group: nope"),
        ("qwen", ["gpt-4o"], "weird", "--type"),
    ],
)
def test_new_body_refusals(model, to, ftype, msg):
    with pytest.raises(ValueError, match=msg):
        fb.new_body(model, to, ftype, {"qwen", "gpt-4o"})


def test_chain_drift_rule_order_free_chain_order_strict_db_only_reported():
    cfg = {"fallbacks": [{"a": ["b", "c"]}, {"d": ["e"]}]}
    live = {"fallbacks": [{"d": ["e"]}, {"a": ["b", "c"]}], "context_window_fallbacks": []}
    assert fb.chain_drift(cfg, live) == []
    live["fallbacks"] = [{"a": ["c", "b"]}, {"d": ["e"]}]
    assert fb.chain_drift(cfg, live) == [{"key": "fallbacks.a", "config": ["b", "c"], "live": ["c", "b"]}]
    # set through POST /fallback: in the DB, not in git
    live = {"fallbacks": [{"a": ["b", "c"]}, {"d": ["e"]}], "context_window_fallbacks": [{"a": ["z"]}]}
    assert fb.chain_drift(cfg, live) == [{"key": "context_window_fallbacks.a", "config": None, "live": ["z"]}]
    # an older proxy that does not report the field is not drift
    assert fb.chain_drift(cfg, {}) == []
    assert fb.chain_drift({"fallbacks": "os.environ/FB"}, {"fallbacks": [{"a": ["b"]}]}) == []


def test_probe_body_sets_the_routers_mock_flag():
    assert fb.probe_body("qwen", "general", "hi") == {
        "model": "qwen",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
        "mock_testing_fallbacks": True,
    }
    assert "mock_testing_context_fallbacks" in fb.probe_body("qwen", "context_window", "x")
    assert "mock_testing_content_policy_fallbacks" in fb.probe_body("qwen", "content_policy", "x")
    with pytest.raises(ValueError):
        fb.probe_body("qwen", "nope", "x")


def test_probe_verdict():
    chain = ["gpt-4o", "claude"]
    v = fb.probe_verdict("qwen", chain, completion_headers("gpt-1", 1), DEPLOYMENTS)
    assert v == {
        "model": "qwen",
        "chain": chain,
        "verdict": "failed_over",
        "served_by": "gpt-4o",
        "deployment_id": "gpt-1",
        "attempted_fallbacks": 1,
    }
    assert fb.probe_verdict("qwen", chain, completion_headers("qwen-a", 0), DEPLOYMENTS)["verdict"] == (
        "not_triggered"
    )
    assert fb.probe_verdict("qwen", chain, {}, DEPLOYMENTS)["verdict"] == "unknown"
    assert fb.probe_verdict("qwen", chain, {"X-LiteLLM-Model-Id": "small-1"}, DEPLOYMENTS)["verdict"] == (
        "unknown"
    )
    assert (
        fb.probe_verdict("qwen", chain, {"x-litellm-attempted-fallbacks": "x"}, [])["attempted_fallbacks"]
        is None
    )


def test_fallback_path_escapes_slashes():
    assert fb.fallback_path("openai/gpt-4o") == "/fallback/openai%2Fgpt-4o"


# ── core/health.py ────────────────────────────────────────────────────────


def test_redact_error_cuts_urls_and_masks_keys():
    out = hc.redact_error(UPSTREAM_ERROR)
    no_secret(out)
    assert "http://n2:8000/…" in out and "Connection refused" in out
    assert hc.redact_error(None) is None and hc.redact_error("") is None
    assert hc.redact_error("x" * 400).endswith("…") and len(hc.redact_error("x" * 400)) == hc.ERROR_MAX
    assert hc.redact_error("timeout after 30s") == "timeout after 30s"


def test_as_rows_tolerates_both_envelopes():
    assert len(hc.as_rows(latest(LATEST_ROWS), "latest_health_checks")) == 4
    assert len(hc.as_rows(history(HISTORY_ROWS), "health_checks")) == 3
    assert hc.as_rows([health_row("q", "1")], "health_checks")[0]["model_id"] == "1"
    assert hc.as_rows(None, "x") == [] and hc.as_rows({"x": "nope"}, "x") == []


def test_normalize_row():
    n = hc.normalize(LATEST_ROWS[1])
    assert n == {
        "model_name": "qwen",
        "model_id": "qwen-b",
        "status": "unhealthy",
        "healthy_count": 0,
        "unhealthy_count": 1,
        "response_time_ms": 812.5,
        "checked_at": "2026-10-25T08:00:00+00:00",
        "checked_by": "background_health_check",
        "error": hc.redact_error(UPSTREAM_ERROR),
    }


def test_latest_rows_joins_live_deployments():
    rows = hc.latest_rows(latest(LATEST_ROWS), DEPLOYMENTS)
    by = {r["model_id"]: r for r in rows}
    assert by["qwen-b"]["live"] is True and by["qwen-b"]["blocked"] is False
    assert by["deleted-1"]["live"] is False  # the row outlived its deployment
    assert by["small-1"]["blocked"] is True
    assert by["gpt-1"]["status"] == "unknown" and by["cfg-claude"]["status"] == "unknown"
    assert [r["status"] for r in rows][:3] == ["unhealthy"] * 3  # unhealthy first
    no_secret(json.dumps(rows))
    # a row recorded by model name only covers that group's deployments
    rows = hc.latest_rows(latest([health_row("qwen", None)]), DEPLOYMENTS[:2])
    assert len(rows) == 1 and rows[0]["live"] is True


def test_history_params_bounds():
    assert hc.history_params("qwen", "unhealthy", 10, 20) == {
        "limit": 10,
        "offset": 20,
        "model": "qwen",
        "status_filter": "unhealthy",
    }
    assert hc.history_params(None, None, 1000, 0) == {"limit": 1000, "offset": 0}
    for args in ((None, None, 0, 0), (None, None, 1001, 0), (None, None, 5, -1), (None, "down", 5, 0)):
        with pytest.raises(ValueError):
            hc.history_params(*args)


# ── core/models.py additions ──────────────────────────────────────────────


def test_model_blocked_helpers():
    assert m.is_blocked(DEPLOYMENTS[4]) is True and m.is_blocked(DEPLOYMENTS[0]) is False
    assert m.serving_replicas(DEPLOYMENTS, "qwen") == 2
    assert m.serving_replicas(DEPLOYMENTS, "qwen", excluding="qwen-a") == 1
    assert m.serving_replicas(DEPLOYMENTS, "small") == 0
    row = next(r for r in m.summarize(DEPLOYMENTS) if r["model_name"] == "small")
    assert row["blocked_replicas"] == 1


def test_resolve_deployment_refusals():
    assert m.resolve_deployment(DEPLOYMENTS, "qwen-a")["model_name"] == "qwen"
    with pytest.raises(ValueError, match=r"model group.*qwen-a, qwen-b"):
        m.resolve_deployment(DEPLOYMENTS, "qwen")
    with pytest.raises(ValueError, match=r"config\.yaml"):
        m.resolve_deployment(DEPLOYMENTS, "cfg-claude")
    with pytest.raises(ValueError, match="models list --deployments"):
        m.resolve_deployment(DEPLOYMENTS, "nope")


# ── core/backend.py ───────────────────────────────────────────────────────


def test_backend_fills_response_headers_on_success_and_error():
    resp = mock.Mock(status_code=200, content=b"{}", headers={"X-LiteLLM-Model-Id": "gpt-1"})
    resp.json.return_value = {}
    conn = {"url": "http://p", "key": "sk-x"}
    with mock.patch("requests.request", return_value=resp):
        got: dict[str, str] = {}
        be.post(conn, "/v1/chat/completions", {}, response_headers=got)
    assert got == {"x-litellm-model-id": "gpt-1"}
    bad = mock.Mock(status_code=500, content=b"{}", headers={"x-litellm-attempted-fallbacks": "2"})
    bad.json.return_value = {"error": {"message": "all fallbacks failed"}}
    with mock.patch("requests.request", return_value=bad), pytest.raises(be.ApiError):
        got = {}
        be.post(conn, "/v1/chat/completions", {}, response_headers=got)
    assert got == {"x-litellm-attempted-fallbacks": "2"}


# ── drift / fleet over fallback chains ────────────────────────────────────


def test_drift_reports_a_db_only_fallback_rule():
    live = router_settings({"fallbacks": [{"qwen": ["gpt-4o"]}]}, {"fallbacks": "db"})["current_values"]
    res = drift.config_vs_live({"router_settings": {"routing_strategy": "simple-shuffle"}}, [], live, [])
    assert res["in_sync"] is False and res["router"] == [
        {"key": "fallbacks.qwen", "config": None, "live": ["gpt-4o"]}
    ]
    cfg = {"router_settings": {"fallbacks": [{"qwen": ["gpt-4o"]}]}}
    assert drift.config_vs_live(cfg, [], live, [])["in_sync"] is True


def test_fleet_diff_compares_fallback_chains():
    snap = {
        "version": "1.106.3",
        "deployments": [],
        "router": router_settings({"fallbacks": [{"qwen": ["gpt-4o"]}]})["current_values"],
        "guardrails": [],
        "keys": 1,
    }
    other = json.loads(json.dumps(snap))
    other["router"]["fallbacks"] = []
    res = drift.fleet_diff({"a": snap, "b": other})
    assert res["identical"] is False
    assert res["proxies"][1]["router"] == [{"key": "fallbacks.qwen", "reference": ["gpt-4o"], "node": None}]
    assert drift.fleet_diff({"a": snap, "b": json.loads(json.dumps(snap))})["identical"] is True


# ── CLI over the mocked proxy ─────────────────────────────────────────────


def proxy(deps=None, settings=None, chains=None, latest_rows=None, history_rows=None):
    """A GET handler for the routes these commands read; records (path, params)."""
    deps = DEPLOYMENTS if deps is None else deps
    settings = settings or router_settings({})
    chains = chains or {}
    calls = []

    def get(c, p, **k):
        calls.append((p, k.get("params")))
        if p == "/v1/model/info":
            return {"data": deps}
        if p == "/router/settings":
            return settings
        if p.startswith("/fallback/"):
            model = p.split("/", 2)[2].replace("%2F", "/")
            ftype = (k.get("params") or {}).get("fallback_type", "general")
            if (model, ftype) not in chains:
                raise be.ApiError(404, no_fallback_text(model, ftype), "http://p" + p)
            return fallback_get(model, chains[(model, ftype)], ftype)
        if p == "/health/latest":
            return latest(LATEST_ROWS if latest_rows is None else latest_rows)
        if p == "/health/history":
            return history(HISTORY_ROWS if history_rows is None else history_rows)
        if p == "/health":
            return {
                "healthy_endpoints": [],
                "unhealthy_endpoints": [],
                "healthy_count": 0,
                "unhealthy_count": 0,
            }
        return {}

    get.calls = calls
    return get


def run(args, get=None, post=None, delete=None, stdin=None):
    with (
        mock.patch.object(be, "get", side_effect=get or proxy()),
        mock.patch.object(be, "post", side_effect=post or (lambda *a, **k: {})),
        mock.patch.object(be, "delete", side_effect=delete or (lambda *a, **k: None)),
    ):
        return CliRunner().invoke(
            cli_mod.cli,
            ["--url", "http://p", "--key", "sk-master", *args],
            input=stdin,
            catch_exceptions=False,
        )


# health


def test_health_without_subcommand_is_unchanged():
    get = proxy()
    r = run(["--json", "health", "--model", "qwen"], get=get)
    assert r.exit_code == 0 and json.loads(r.output)["healthy_count"] == 0
    assert ("/health", {"model": "qwen"}) in get.calls


def test_health_model_before_a_subcommand_is_a_usage_error():
    r = run(["health", "--model", "qwen", "latest"])
    assert r.exit_code == 2 and "health history --model" in r.output


def test_health_latest_exit_2_on_live_unhealthy_and_no_backend_call():
    get = proxy()
    r = run(["--json", "health", "latest"], get=get)
    rows = json.loads(r.output)
    assert r.exit_code == 2
    assert [p for p, _ in get.calls] == ["/health/latest", "/v1/model/info"]  # never /health
    assert {x["model_id"]: x["status"] for x in rows}["gpt-1"] == "unknown"
    no_secret(r.output)


def test_health_latest_gone_and_paused_rows_do_not_fail():
    rows = [health_row("qwen", "qwen-a"), LATEST_ROWS[2], LATEST_ROWS[3]]
    r = run(["health", "latest", "--unhealthy"], get=proxy(latest_rows=rows))
    assert r.exit_code == 0
    assert "STATUS" in r.output and "gone" in r.output and "blocked" in r.output
    assert "qwen-a" not in r.output  # --unhealthy hides healthy rows


def test_health_history_query_paging_hint_and_redaction():
    get = proxy()
    r = run(["health", "history", "--model", "qwen", "--status", "unhealthy", "--limit", "3"], get=get)
    assert r.exit_code == 0 and "WHEN" in r.output and "(more: --offset 3)" in r.output
    assert ("/health/history", {"limit": 3, "offset": 0, "model": "qwen", "status_filter": "unhealthy"}) in (
        get.calls
    )
    no_secret(r.output)
    rows = json.loads(run(["--json", "health", "history"]).output)
    assert [x["status"] for x in rows] == ["unhealthy", "healthy", "healthy"]


def test_health_history_limit_refused_before_the_proxy():
    get = proxy()
    r = run(["health", "history", "--limit", "5000"], get=get)
    assert r.exit_code == 1 and "1..1000" in r.output and get.calls == []


# models block / unblock


def test_models_list_shows_paused():
    r = run(["--json", "models", "list", "--deployments"])
    rows = {x["id"]: x for x in json.loads(r.output)}
    assert rows["small-1"]["blocked"] is True and rows["qwen-a"]["blocked"] is False
    assert "PAUSED" in run(["models", "list", "--deployments"]).output
    groups = {x["model_name"]: x for x in json.loads(run(["--json", "models", "list"]).output)}
    assert groups["small"]["blocked_replicas"] == 1


def test_block_dry_run_gating_and_no_row_echo():
    post = mock.Mock(return_value=blocked_row("qwen-b", "qwen", True))
    r = run(["--dry-run", "models", "block", "qwen-b"], post=post)
    assert json.loads(r.output) == {
        "dry_run": True,
        "would": "POST /model/block",
        "body": {"model_id": "qwen-b"},
    }
    post.assert_not_called()
    r = run(["models", "block", "qwen-b"], post=post)
    assert r.exit_code == 1 and "without --yes" in r.output and "1 other replica(s) of qwen" in r.output
    post.assert_not_called()
    r = run(["--json", "models", "block", "qwen-b", "--yes"], post=post)
    out = json.loads(r.output)
    assert out == {
        "model_id": "qwen-b",
        "model_name": "qwen",
        "blocked": True,
        "serving_replicas": 1,
        "changed": True,
    }
    assert post.call_args.args[1:] == ("/model/block", {"model_id": "qwen-b"})
    no_secret(r.output)


def test_block_last_replica_says_so():
    deps = [deployment("solo-1", "solo")]
    r = run(["models", "block", "solo-1"], get=proxy(deps=deps))
    assert "LAST serving replica" in r.output


def test_block_refuses_groups_config_and_unknown_before_the_proxy():
    post = mock.Mock()
    for ref, hint in (("qwen", "one deployment id"), ("cfg-claude", "config.yaml"), ("x", "--deployments")):
        r = run(["models", "block", ref, "--yes"], post=post)
        assert r.exit_code == 1 and hint in r.output
    post.assert_not_called()


def test_unblock_is_idempotent_and_ungated():
    post = mock.Mock(return_value=blocked_row("small-1", "small", False))
    r = run(["--json", "models", "unblock", "qwen-a"], post=post)
    assert json.loads(r.output)["changed"] is False
    post.assert_not_called()
    r = run(["--json", "models", "unblock", "small-1"], post=post)
    out = json.loads(r.output)
    assert out["blocked"] is False and out["serving_replicas"] == 1 and out["changed"] is True
    assert post.call_args.args[1] == "/model/unblock"


def test_block_proxy_errors_surface():
    def post(c, p, body=None, **k):
        raise be.ApiError(403, "Only proxy admins can change a model's blocked flag.", "http://p" + p)

    r = run(["models", "block", "qwen-a", "--yes"], post=post)
    assert r.exit_code == 1 and "Only proxy admins" in r.output


# router show / fallbacks list / show


SETTINGS = router_settings(
    {
        "fallbacks": [{"qwen": ["gpt-4o", "claude"]}, {"*": ["small"]}],
        "context_window_fallbacks": [{"qwen": ["claude"]}],
    },
    {"fallbacks": "db", "context_window_fallbacks": "config"},
)


def test_router_show_includes_fallbacks():
    out = json.loads(run(["--json", "router", "show"], get=proxy(settings=SETTINGS)).output)
    assert out["fallbacks"] == [{"qwen": ["gpt-4o", "claude"]}, {"*": ["small"]}]
    assert "routing_groups" not in out


def test_fallbacks_list_table_and_json():
    r = run(["fallbacks", "list"], get=proxy(settings=SETTINGS))
    assert r.exit_code == 0 and "gpt-4o → claude" in r.output and "SRC" in r.output
    rows = json.loads(
        run(["--json", "fallbacks", "list", "--type", "context_window"], get=proxy(settings=SETTINGS)).output
    )
    assert rows == [{"model": "qwen", "type": "context_window", "fallbacks": ["claude"], "source": "config"}]


def test_fallbacks_show_effective_chain_and_none():
    get = proxy(chains={("qwen", "general"): ["gpt-4o", "claude"]})
    out = json.loads(run(["--json", "fallbacks", "show", "qwen"], get=get).output)
    assert out == {"model": "qwen", "fallback_type": "general", "fallback_models": ["gpt-4o", "claude"]}
    assert ("/fallback/qwen", {"fallback_type": "general"}) in get.calls
    r = run(["fallbacks", "show", "gpt-4o", "--type", "content_policy"], get=get)
    assert r.exit_code == 0 and "(none)" in r.output


def test_fallbacks_show_other_errors_are_errors():
    def get(c, p, **k):
        raise be.ApiError(401, "bad key", "http://p" + p)

    r = run(["fallbacks", "show", "qwen"], get=get)
    assert r.exit_code == 1 and "master keys differ" in r.output


# fallbacks set / delete


def test_fallbacks_set_dry_run_exact_body():
    post = mock.Mock()
    r = run(
        ["--dry-run", "fallbacks", "set", "gpt-4o", "--to", "claude,qwen"],
        get=proxy(settings=SETTINGS),
        post=post,
    )
    assert json.loads(r.output) == {
        "dry_run": True,
        "would": "POST /fallback",
        "body": {"model": "gpt-4o", "fallback_models": ["claude", "qwen"], "fallback_type": "general"},
    }
    post.assert_not_called()


def test_fallbacks_set_posts_and_names_the_replaced_chain():
    post = mock.Mock(side_effect=lambda c, p, body=None, **k: fallback_created(body, updated=True))
    r = run(["--json", "fallbacks", "set", "qwen", "--to", "claude"], get=proxy(settings=SETTINGS), post=post)
    out = json.loads(r.output)
    assert out["replaced"] == ["gpt-4o", "claude"] and out["fallback_models"] == ["claude"]
    assert out["message"] == "Fallback configuration updated successfully"
    assert post.call_args.args[1] == "/fallback"
    r = run(["fallbacks", "set", "qwen", "--to", "claude"], get=proxy(settings=SETTINGS), post=post)
    assert "(was gpt-4o → claude)" in r.output


def test_fallbacks_set_refusals_before_the_proxy():
    post = mock.Mock()
    get = proxy(settings=SETTINGS)
    cases = [
        (["fallbacks", "set", "qwen", "--to", "nope"], "not a live model group"),
        (["fallbacks", "set", "qwen", "--to", "qwen"], "its own fallback"),
        (["fallbacks", "set", "qwen", "--to", "claude", "--type", "context_window"], "config.yaml"),
    ]
    for args, hint in cases:
        r = run(args, get=get, post=post)
        assert r.exit_code == 1 and hint in r.output, args
    post.assert_not_called()


def test_fallbacks_set_store_model_in_db_off_surfaces():
    def post(c, p, body=None, **k):
        raise be.ApiError(400, "Database storage not enabled. Set 'STORE_MODEL_IN_DB=True'", "http://p" + p)

    r = run(["fallbacks", "set", "gpt-4o", "--to", "qwen"], get=proxy(settings=SETTINGS), post=post)
    assert r.exit_code == 1 and "STORE_MODEL_IN_DB" in r.output


def test_fallbacks_delete_gating_dry_run_and_catch_all_note():
    dele = mock.Mock(return_value=fallback_deleted("qwen"))
    get = proxy(settings=SETTINGS)
    r = run(["fallbacks", "delete", "qwen"], get=get, delete=dele)
    assert r.exit_code == 1 and "without --yes" in r.output
    r = run(["--dry-run", "fallbacks", "delete", "qwen"], get=get, delete=dele)
    assert json.loads(r.output)["would"] == "DELETE /fallback/qwen?fallback_type=general"
    dele.assert_not_called()
    r = run(["--json", "fallbacks", "delete", "qwen", "--yes"], get=get, delete=dele)
    assert json.loads(r.output) == {
        "deleted": "qwen",
        "fallback_type": "general",
        "fallbacks": ["gpt-4o", "claude"],
        "still_covered_by": ["small"],
    }
    assert dele.call_args.args[1] == "/fallback/qwen" and dele.call_args.kwargs["params"] == {
        "fallback_type": "general"
    }


def test_fallbacks_delete_unknown_and_config_owned_refused():
    dele = mock.Mock()
    get = proxy(settings=SETTINGS)
    r = run(["fallbacks", "delete", "gpt-4o", "--yes"], get=get, delete=dele)
    assert r.exit_code == 1 and "fallbacks list" in r.output
    r = run(["fallbacks", "delete", "qwen", "--type", "context_window", "--yes"], get=get, delete=dele)
    assert r.exit_code == 1 and "config.yaml" in r.output
    dele.assert_not_called()


# fallbacks test


def _completion_post(dep_id, attempted=1):
    def post(c, p, body=None, response_headers=None, **k):
        assert p == "/v1/chat/completions" and body["mock_testing_fallbacks"] is True
        if response_headers is not None:
            response_headers.update(completion_headers(dep_id, attempted))
        return COMPLETION

    return post


def test_fallbacks_test_failed_over():
    get = proxy(chains={("qwen", "general"): ["gpt-4o", "claude"]})
    r = run(["--json", "fallbacks", "test", "qwen"], get=get, post=_completion_post("gpt-1"))
    out = json.loads(r.output)
    assert r.exit_code == 0 and out["verdict"] == "failed_over" and out["served_by"] == "gpt-4o"
    r = run(["fallbacks", "test", "qwen"], get=get, post=_completion_post("gpt-1"))
    assert "served by gpt-4o (gpt-1)" in r.output


def test_fallbacks_test_not_triggered_and_chain_failed_exit_2():
    get = proxy(chains={("qwen", "general"): ["gpt-4o"]})
    r = run(["--json", "fallbacks", "test", "qwen"], get=get, post=_completion_post("qwen-a", 0))
    assert r.exit_code == 2 and json.loads(r.output)["verdict"] == "not_triggered"

    def post(c, p, body=None, **k):
        raise be.ApiError(500, UPSTREAM_ERROR, "http://p" + p)

    r = run(["--json", "fallbacks", "test", "qwen"], get=get, post=post)
    out = json.loads(r.output)
    assert r.exit_code == 2 and out["verdict"] == "chain_failed" and out["status"] == 500
    no_secret(r.output)


def test_fallbacks_test_without_chain_or_with_bad_key_is_an_error():
    post = mock.Mock()
    r = run(["fallbacks", "test", "gpt-4o"], get=proxy(), post=post)
    assert r.exit_code == 1 and "nothing to test" in r.output
    post.assert_not_called()

    def bad(c, p, body=None, **k):
        raise be.ApiError(401, "bad key", "http://p" + p)

    r = run(["fallbacks", "test", "qwen"], get=proxy(chains={("qwen", "general"): ["gpt-4o"]}), post=bad)
    assert r.exit_code == 1 and "chain_failed" not in r.output


def test_fallbacks_test_dry_run_sends_no_completion():
    post = mock.Mock()
    get = proxy(chains={("qwen", "context_window"): ["claude"]})
    r = run(["--dry-run", "fallbacks", "test", "qwen", "--type", "context_window"], get=get, post=post)
    body = json.loads(r.output)["body"]
    assert body["mock_testing_context_fallbacks"] is True
    post.assert_not_called()


def test_drift_command_exit_2_on_db_fallback(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model_list: []\nrouter_settings:\n  routing_strategy: simple-shuffle\n")
    get = proxy(deps=[], settings=router_settings({"fallbacks": [{"qwen": ["gpt-4o"]}]}, {"fallbacks": "db"}))
    r = run(["drift", "--config", str(cfg)], get=get)
    assert r.exit_code == 2 and "fallbacks.qwen" in r.output


# ── workflows ─────────────────────────────────────────────────────────────


def test_workflow_backend_incident():
    """A replica goes bad: see it without paying for a health sweep, pause it,
    make sure the group can still fail over, then put it back."""
    state = {"deps": [dict(d, model_info=dict(d["model_info"])) for d in DEPLOYMENTS], "fallbacks": []}

    def get(c, p, **k):
        if p == "/router/settings":
            return router_settings({"fallbacks": state["fallbacks"]}, {"fallbacks": "db"})
        if p.startswith("/fallback/"):
            chain = fb.rules(state["fallbacks"]).get(p.split("/")[2])
            if not chain:
                raise be.ApiError(404, no_fallback_text(p.split("/")[2]), "http://p" + p)
            return fallback_get(p.split("/")[2], chain)
        return proxy(deps=state["deps"])(c, p, **k)

    def post(c, p, body=None, response_headers=None, **k):
        if p in ("/model/block", "/model/unblock"):
            d = next(x for x in state["deps"] if x["model_info"]["id"] == body["model_id"])
            d["model_info"]["blocked"] = p == "/model/block"
            return blocked_row(body["model_id"], d["model_name"], d["model_info"]["blocked"])
        if p == "/fallback":
            state["fallbacks"] = [{body["model"]: body["fallback_models"]}]
            return fallback_created(body)
        if p == "/v1/chat/completions":
            response_headers.update(completion_headers("gpt-1", 1))
            return COMPLETION
        raise AssertionError(p)

    # 1. recorded health names the bad replica (exit 2), no backend called
    r = run(["--json", "health", "latest"], get=get, post=post)
    bad = [x for x in json.loads(r.output) if x["status"] == "unhealthy" and x["live"] and not x["blocked"]]
    assert r.exit_code == 2 and [x["model_id"] for x in bad] == ["qwen-b"]
    # 2. its history: when it went down
    r = run(["--json", "health", "history", "--model", "qwen", "--status", "unhealthy"], get=get, post=post)
    assert json.loads(r.output)[0]["checked_at"] == "2026-10-25T08:10:00+00:00"
    # 3. pause it; one replica keeps serving
    r = run(["--json", "models", "block", "qwen-b", "--yes"], get=get, post=post)
    assert json.loads(r.output)["serving_replicas"] == 1
    # 4. with one replica left, give the group a fallback, then prove it serves
    run(["fallbacks", "set", "qwen", "--to", "gpt-4o"], get=get, post=post)
    assert json.loads(run(["--json", "fallbacks", "list"], get=get, post=post).output)[0]["fallbacks"] == [
        "gpt-4o"
    ]
    r = run(["--json", "fallbacks", "test", "qwen"], get=get, post=post)
    assert r.exit_code == 0 and json.loads(r.output)["served_by"] == "gpt-4o"
    # 5. recorded health no longer fails on the paused replica
    r = run(["health", "latest"], get=get, post=post)
    assert r.exit_code == 0 and "blocked" in r.output
    # 6. fixed: back into rotation
    r = run(["--json", "models", "unblock", "qwen-b"], get=get, post=post)
    assert json.loads(r.output)["serving_replicas"] == 2


def test_workflow_fallback_set_through_api_shows_as_drift_until_in_git(tmp_path):
    state = {"fallbacks": []}

    def get(c, p, **k):
        if p == "/router/settings":
            return router_settings({"fallbacks": state["fallbacks"]}, {"fallbacks": "db"})
        return proxy(deps=[])(c, p, **k)

    def post(c, p, body=None, **k):
        state["fallbacks"] = [{body["model"]: body["fallback_models"]}]
        return fallback_created(body)

    cfg = tmp_path / "config.yaml"
    cfg.write_text("model_list: []\n")
    assert run(["drift", "--config", str(cfg)], get=get).exit_code == 0
    with mock.patch.object(cli_mod, "live_deployments", return_value=DEPLOYMENTS):
        assert run(["fallbacks", "set", "qwen", "--to", "gpt-4o,claude"], get=get, post=post).exit_code == 0
    r = run(["--json", "drift", "--config", str(cfg)], get=get)
    assert r.exit_code == 2
    assert json.loads(r.output)["router"] == [
        {"key": "fallbacks.qwen", "config": None, "live": ["gpt-4o", "claude"]}
    ]
    cfg.write_text("model_list: []\nrouter_settings:\n  fallbacks:\n    - qwen: [gpt-4o, claude]\n")
    assert run(["drift", "--config", str(cfg)], get=get).exit_code == 0


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


class TestResilienceSubprocess:
    CLI_BASE = _resolve_cli("cli-anything-litellm")

    def _run(self, args):
        import os
        import subprocess

        env = {**os.environ, "LITELLM_URL": "http://127.0.0.1:9", "LITELLM_API_KEY": "sk-test"}
        return subprocess.run(self.CLI_BASE + args, capture_output=True, text=True, env=env, timeout=60)

    def test_help_lists_the_new_verbs(self):
        r = self._run(["fallbacks", "--help"])
        assert r.returncode == 0
        for verb in ("list", "show", "set", "delete", "test"):
            assert verb in r.stdout
        r = self._run(["health", "--help"])
        assert r.returncode == 0 and "latest" in r.stdout and "history" in r.stdout
        r = self._run(["models", "--help"])
        assert "block" in r.stdout and "unblock" in r.stdout

    def test_unreachable_proxy_fails_loudly(self):
        r = self._run(["--json", "health", "latest"])
        assert r.returncode == 1 and "127.0.0.1:9" in r.stderr

    def test_history_bound_refused_without_a_proxy(self):
        r = self._run(["health", "history", "--limit", "0"])
        assert r.returncode == 1 and "1..1000" in r.stderr
