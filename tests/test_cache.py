"""The response cache (v0.13.0): ping, redis info, settings, invalidation.

Core units, the CLI over a mocked proxy, three workflows and the installed
command. Fixtures (tests/cache_fixtures.py) reproduce LiteLLM v1.106.3
response shapes; each cites the function it mirrors. None of the endpoints
could be checked live (proxy-admin only — see the fixtures module), so every
error path here is derived from the v1.106.3 source.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
from click.testing import CliRunner

from cli_anything.litellm import litellm_cli as cli_mod
from cli_anything.litellm.core import backend as be
from cli_anything.litellm.core import cache as ca

from cache_fixtures import (
    REDIS_PASSWORD,
    admin_denied_body,
    cache_delete_failed_text,
    cache_delete_ok,
    cache_test_failed,
    cache_test_not_redis,
    cache_test_ok,
    ping_local,
    ping_no_cache_text,
    ping_redis,
    proxy_mask,
    redis_info,
    service_unhealthy_text,
    settings_response,
    settings_updated,
    store_model_required_text,
)

SECRETS = ("SECRET", REDIS_PASSWORD, "SuperSecret")


def no_secret(text: str) -> None:
    for s in SECRETS:
        assert s not in text, s


# ── core/cache.py ─────────────────────────────────────────────────────────


def test_parse_json_object_tolerates_string_dict_and_junk():
    assert ca.parse_json_object('{"type": "redis"}') == {"type": "redis"}
    assert ca.parse_json_object({"type": "local"}) == {"type": "local"}
    assert ca.parse_json_object("not json") == {}
    assert ca.parse_json_object(None) == {}
    assert ca.parse_json_object(["x"]) == {}


def test_redact_params_masks_harder_than_the_proxy():
    """The proxy's mask keeps 4+4 characters of a credential; the CLI masks fully."""
    params = {
        "type": "redis",
        "password": proxy_mask(REDIS_PASSWORD),
        "redis_kwargs": {"password": proxy_mask(REDIS_PASSWORD), "db": 0},
        "url": "redis://:sneaky@redis.litellm.svc:6379/1",
        "namespace": "litellm_proxy",
    }
    out = ca.redact_params(params)
    assert out["password"] == ca.MASK
    assert out["redis_kwargs"]["password"] == ca.MASK
    assert out["redis_kwargs"]["db"] == 0
    assert out["url"] == "redis://redis.litellm.svc:6379/…"  # origin, userinfo gone
    assert out["namespace"] == "litellm_proxy"
    no_secret(json.dumps(out))
    # markers pass through (they stand for stored secrets, never for values)
    assert ca.redact_params({"password": ca.REDACTED, "url": ca.REDACTED}) == {
        "password": ca.REDACTED,
        "url": ca.REDACTED,
    }
    assert ca.redact_params([{"password": "x"}]) == [{"password": ca.MASK}]
    assert ca.redact_params("plain") == "plain"
    assert ca._is_sensitive(None) is False  # a non-string key is never sensitive


def test_ping_row_parses_the_json_string_params():
    row = ca.ping_row(ping_redis())
    assert row["status"] == "healthy" and row["cache_type"] == "redis"
    assert row["ping_response"] is True and row["set_cache_response"] == "success"
    assert row["params"]["type"] == "redis" and row["params"]["ttl"] == 300.0
    assert row["health_check_params"]["redis_version"] == "7.2.4"
    no_secret(json.dumps(row))
    local = ca.ping_row(ping_local())
    assert local["ping_response"] is None and local["set_cache_response"] is None
    assert local["params"]["type"] == "local"
    assert ca.ping_row(None) == {
        "status": None,
        "cache_type": None,
        "ping_response": None,
        "set_cache_response": None,
        "params": {},
        "health_check_params": {},
    }


def test_info_summary_sums_keyspace_and_rates():
    row = ca.info_row(redis_info())
    s = row["summary"]
    assert s["total_keys"] == 4330  # db0 + db1
    assert s["keyspace_hits"] == 8213 and s["keyspace_misses"] == 1764
    assert s["hit_rate_percent"] == 82  # 8213 / (8213 + 1764)
    assert s["maxmemory"] == "0B" and s["maxmemory_policy"] == "noeviction"
    assert row["num_clients"] == 3 and len(row["clients"]) == 3
    no_clients = ca.info_row(redis_info(no_clients=True))
    assert no_clients["num_clients"] == -1
    assert no_clients["summary"]["total_keys"] == 4330
    empty = ca.info_summary(None)
    assert empty["total_keys"] == 0 and empty["hit_rate_percent"] is None


def test_info_summary_number_coercion_and_junk():
    """INFO numbers can arrive as strings, and a rate that cannot be computed is None."""
    s = ca.info_summary({"info": {"keyspace_hits": "82", "keyspace_misses": "18", "db0": {"keys": "7"}}})
    assert s["keyspace_hits"] == 82 and s["keyspace_misses"] == 18
    assert s["hit_rate_percent"] == 82 and s["total_keys"] == 7
    junk = ca.info_summary({"info": {"keyspace_hits": 5, "keyspace_misses": "?"}})
    assert junk["hit_rate_percent"] is None  # not guessed
    assert ca.info_summary({"info": {"keyspace_hits": 0, "keyspace_misses": 0}})["hit_rate_percent"] is None
    assert ca.info_summary({"info": {"keyspace_hits": 0, "keyspace_misses": 4}})["hit_rate_percent"] == 0
    assert ca._num(True) is True and ca._num(7) == 7 and ca._num(7.5) == 7.5 and ca._num(None) is None


def test_settings_row_keeps_markers_and_metadata():
    row = ca.settings_row(settings_response())
    assert row["current_values"]["password"] == ca.REDACTED
    assert row["current_values"]["redis_type"] == "node"  # derived for the UI
    assert row["redis_type_descriptions"]["node"].startswith("Standard Redis")
    assert row["fields"][0]["field_name"] == "redis_type"
    no_secret(json.dumps(row))


def test_parse_sets_types_every_declared_field():
    assert ca.parse_sets(("ttl=300", "port=6379", "ssl=true", "ssl_check_hostname=false")) == {
        "ttl": 300.0,
        "port": 6379,
        "ssl": True,
        "ssl_check_hostname": False,
    }
    nodes = ca.parse_sets(('redis_startup_nodes=[{"host": "h", "port": "7001"}]', "host=r2"))
    assert nodes["redis_startup_nodes"] == [{"host": "h", "port": "7001"}]
    assert ca.parse_sets(("namespace=litellm_proxy",))["namespace"] == "litellm_proxy"
    assert ca.parse_sets(("password=os.environ/REDIS_PASSWORD",)) == {"password": "os.environ/REDIS_PASSWORD"}
    # a url WITHOUT userinfo is just a connection string, not a credential
    assert ca.parse_sets(("url=redis://r2:6379/1",)) == {"url": "redis://r2:6379/1"}
    assert ca.parse_sets(("url=os.environ/REDIS_URL",)) == {"url": "os.environ/REDIS_URL"}
    # a malformed url cannot be inspected for userinfo — accepted as-is, the proxy decides
    assert ca.parse_sets(("url=http://[bad",)) == {"url": "http://[bad"}


@pytest.mark.parametrize(
    ("pairs", "msg"),
    [
        (("ttl",), "key=value"),
        (("=300",), "key=value"),
        (("redis_type=cluster",), "derives it"),
        (("password=hunter2",), "os.environ/NAME"),
        (("sentinel_password=hunter2",), "os.environ/NAME"),
        (("url=redis://:pass@h:6379/1",), "os.environ/NAME"),
        (("ssl=maybe",), "Boolean"),
        (("port=abc",), "Integer"),
        (("ttl=abc",), "Float"),
        (('redis_startup_nodes={"host": "h"}',), "JSON array"),
        (('redis_startup_nodes="not json"',), "JSON array"),
        (("redis_startup_nodes=not json",), "JSON array"),
    ],
)
def test_parse_sets_refusals(pairs, msg):
    with pytest.raises(ValueError, match=msg):
        ca.parse_sets(pairs)


def test_build_body_merges_over_current_and_drops_derived():
    current = {"type": "redis", "host": "r1", "port": "6379", "password": ca.REDACTED, "redis_type": "node"}
    body = ca.build_body(current, {"ttl": 60.0})
    assert body == {
        "cache_settings": {
            "type": "redis",
            "host": "r1",
            "port": "6379",
            "password": ca.REDACTED,  # the marker rides: the proxy maps it back to the secret
            "ttl": 60.0,
        }
    }
    assert "redis_type" not in body["cache_settings"]  # UI-only, ignored on init


def test_dropped_credentials_names_what_a_target_change_loses():
    current = {"host": "r1", "port": "6379", "password": ca.REDACTED, "sentinel_password": None}
    change_host = ca.build_body(current, {"host": "r2"})
    assert ca.dropped_credentials(change_host, current) == ["password"]  # anti-replay
    same_target = ca.build_body(current, {"ttl": 60.0})
    assert ca.dropped_credentials(same_target, current) == []
    with_new_password = ca.build_body(current, {"host": "r2", "password": "os.environ/P"})
    assert ca.dropped_credentials(with_new_password, current) == []
    # a pinned port that changes also blocks credential reuse (targets compare
    # in canonical form: a list/dict target json-sorts, a port "6379" vs 6379 matches)
    change_port = ca.build_body({"port": "6379", "password": ca.REDACTED}, {"port": 6380})
    assert ca.dropped_credentials(change_port, {"port": "6379", "password": ca.REDACTED}) == ["password"]
    same_port_spelling = ca.build_body({"port": "6379", "password": ca.REDACTED}, {"port": 6379})
    assert ca.dropped_credentials(same_port_spelling, {"port": "6379", "password": ca.REDACTED}) == []
    nodes_current = {
        "redis_startup_nodes": [{"host": "h1", "port": "7001"}],
        "password": ca.REDACTED,
    }
    same_nodes = ca.build_body(nodes_current, {"redis_startup_nodes": [{"port": "7001", "host": "h1"}]})
    assert ca.dropped_credentials(same_nodes, nodes_current) == []  # key order does not matter
    other_nodes = ca.build_body(nodes_current, {"redis_startup_nodes": [{"host": "h2", "port": "7001"}]})
    assert ca.dropped_credentials(other_nodes, nodes_current) == ["password"]

    url_current = {"url": ca.REDACTED, "ttl": 300.0}
    to_discrete = ca.build_body(url_current, {"host": "r2"})
    assert ca.dropped_credentials(to_discrete, url_current) == ["url"]  # url precedence drops it
    keep_url = ca.build_body(url_current, {"ttl": 60.0})
    assert ca.dropped_credentials(keep_url, url_current) == []
    # url pinned and changed without a new credential: the old one is not replayed
    changed_url = ca.build_body(url_current, {"url": "redis://r2:6379"})
    assert ca.dropped_credentials(changed_url, url_current) == []
    # an unredacted stored url re-sent unchanged: same target, the credential survives
    raw_url = {"url": "redis://r2:6379", "password": ca.REDACTED}
    same_url = ca.build_body(raw_url, {"ttl": 60.0})
    assert ca.dropped_credentials(same_url, raw_url) == []
    # nothing stored to lose: no markers in current
    assert ca.dropped_credentials(ca.build_body({"host": "r1"}, {"host": "r2"}), {"host": "r1"}) == []


def test_empty_save_flags_a_config_owned_cache():
    assert ca.empty_save({}, {"ttl": 300.0}) is True  # no row, no env: config.yaml owns it
    assert ca.empty_save({"redis_type": "node"}, {"ttl": 300.0}) is True  # derived fields don't count
    assert ca.empty_save({"host": "r1"}, {"ttl": 300.0}) is False  # a row (or env) exists
    assert ca.empty_save({}, {}) is False


def test_delete_body_requires_a_key():
    assert ca.delete_body((" litellm_proxy:abc ", "litellm_proxy:def")) == {
        "keys": ["litellm_proxy:abc", "litellm_proxy:def"]
    }
    for keys in ((), ("", " ")):
        with pytest.raises(ValueError, match="at least one cache key"):
            ca.delete_body(keys)


# ── CLI over the mocked proxy ─────────────────────────────────────────────


def proxy(ping=None, info=None, settings=None):
    """GET handlers for the routes these commands read; records (path, params)."""
    calls = []

    def get(c, p, **k):
        calls.append((p, k.get("params")))
        if p == "/cache/ping":
            if isinstance(ping, be.ApiError):
                raise ping
            return ping if ping is not None else ping_redis()
        if p == "/cache/redis/info":
            if isinstance(info, be.ApiError):
                raise info
            return info if info is not None else redis_info()
        if p == "/cache/settings":
            if isinstance(settings, be.ApiError):
                raise settings
            return settings if settings is not None else settings_response()
        return {}

    get.calls = calls
    return get


def run(args, get=None, post=None, stdin=None):
    with (
        mock.patch.object(be, "get", side_effect=get or proxy()),
        mock.patch.object(be, "post", side_effect=post or (lambda *a, **k: {})),
    ):
        return CliRunner().invoke(
            cli_mod.cli,
            ["--url", "http://p", "--key", "sk-admin", *args],
            input=stdin,
            catch_exceptions=False,
        )


# ping


def test_ping_healthy_human_and_json():
    r = run(["cache", "ping"])
    assert r.exit_code == 0
    assert (
        "healthy" in r.output and "redis" in r.output and "ping ok" in r.output and "set success" in r.output
    )
    assert "backend cache:" in r.output and "redis.litellm.svc" in r.output  # the connection lives there
    r = run(["--json", "cache", "ping"])
    assert r.exit_code == 0
    out = json.loads(r.output)
    assert out["params"]["type"] == "redis"
    assert out["health_check_params"]["redis_kwargs"]["host"] == "redis.litellm.svc"
    no_secret(r.output)


def test_ping_unhealthy_503_exits_2_with_redacted_error():
    err = be.ApiError(503, ping_no_cache_text(), "http://p/cache/ping")
    r = run(["cache", "ping"], get=proxy(ping=err))
    assert r.exit_code == 2 and "unhealthy" in r.output
    assert "Cache not initialized" in r.output
    r = run(["--json", "cache", "ping"], get=proxy(ping=err))
    assert r.exit_code == 2 and json.loads(r.output)["status"] == "unhealthy"
    no_secret(r.output)


def test_ping_admin_denied_surfaces_the_live_401():
    body = admin_denied_body("/cache/ping")

    def get(c, p, **k):
        raise be.ApiError(401, body["error"]["message"], "http://p" + p)

    r = run(["cache", "ping"], get=get)
    assert r.exit_code == 1 and "Only proxy admin" in r.output
    assert body["error"]["code"] == "401"  # the captured live shape matches what _error_text reads


def test_ping_local_cache_has_no_ping_fields():
    r = run(["cache", "ping"], get=proxy(ping=ping_local()))
    assert r.exit_code == 0 and "no ping" in r.output and "no set" in r.output


# info


def test_info_human_curates_the_operator_view():
    r = run(["cache", "info"])
    assert r.exit_code == 0
    for needle in ("redis_version", "7.2.4", "12.50M", "0B", "noeviction", "4330", "82"):
        assert needle in r.output
    r = run(["--json", "cache", "info"])
    out = json.loads(r.output)
    assert out["num_clients"] == 3 and out["summary"]["total_keys"] == 4330
    assert out["info"]["db1"]["keys"] == 9  # the full INFO dict rides in --json


def test_info_client_list_unavailable_is_flagged():
    r = run(["cache", "info"], get=proxy(info=redis_info(no_clients=True)))
    assert r.exit_code == 0 and "CLIENT LIST is not available" in r.output


def test_info_every_failure_is_a_wrapped_503():
    err = be.ApiError(
        503, service_unhealthy_text("500: Cache type local does not support redis info"), "http://p"
    )
    r = run(["cache", "info"], get=proxy(info=err))
    assert r.exit_code == 1 and "Service Unhealthy" in r.output
    no_cache = be.ApiError(
        503, service_unhealthy_text("503: Cache not initialized. litellm.cache is None"), "http://p"
    )
    r = run(["cache", "info"], get=proxy(info=no_cache))
    assert r.exit_code == 1 and "Cache not initialized" in r.output


# settings show


def test_settings_show_table_and_json():
    r = run(["cache", "settings"])
    assert r.exit_code == 0
    assert "redis.litellm.svc" in r.output and "***REDACTED***" in r.output
    r = run(["--json", "cache", "settings"])
    out = json.loads(r.output)
    assert out["current_values"]["password"] == ca.REDACTED
    assert out["current_values"]["redis_type"] == "node"
    no_secret(r.output)


def test_settings_derives_sentinel_and_cluster_types():
    sentinel = settings_response({"sentinel_nodes": [["localhost", 26379]], "service_name": "mymaster"})
    r = run(["--json", "cache", "settings"], get=proxy(settings=sentinel))
    assert json.loads(r.output)["current_values"]["redis_type"] == "sentinel"
    cluster = settings_response({"redis_startup_nodes": [{"host": "h", "port": "7001"}]})
    r = run(["--json", "cache", "settings"], get=proxy(settings=cluster))
    assert json.loads(r.output)["current_values"]["redis_type"] == "cluster"


def test_settings_url_precedence_hides_discrete_fields():
    """A stored url wins over discrete fields (the proxy drops host/port on
    read), and the url itself always comes back as the redaction marker."""
    url = settings_response({"url": "redis://:secret@r2:6379/1"})
    url["current_values"]["host"] = None  # _resolve_cache_url_precedence drops them
    url["current_values"]["port"] = None
    r = run(["--json", "cache", "settings"], get=proxy(settings=url))
    cv = json.loads(r.output)["current_values"]
    assert cv["url"] == ca.REDACTED and cv["host"] is None


# settings test


def test_settings_test_merges_over_current_and_exits_2_on_failure():
    post = mock.Mock(return_value=cache_test_ok())
    r = run(
        ["--json", "cache", "settings", "test", "--set", "ttl=60"],
        get=proxy(),
        post=post,
    )
    assert r.exit_code == 0
    sent = post.call_args.args[1:]
    assert sent[0] == "/cache/settings/test"
    assert sent[1]["cache_settings"]["ttl"] == 60.0
    assert sent[1]["cache_settings"]["host"] == "redis.litellm.svc"
    assert sent[1]["cache_settings"]["password"] == ca.REDACTED  # the marker rides
    out = json.loads(r.output)
    assert out["status"] == "success" and out["changes"] == {"ttl": 60.0}
    no_secret(r.output)


def test_settings_test_failed_exits_2():
    post = mock.Mock(return_value=cache_test_failed("Error 111 connecting to r2:6379. Connection refused."))
    r = run(["cache", "settings", "test", "--set", "host=r2"], get=proxy(), post=post)
    assert r.exit_code == 2 and "failed" in r.output and "Connection refused" in r.output


def test_settings_test_not_redis_surfaces_the_proxy_message():
    post = mock.Mock(return_value=cache_test_not_redis())
    r = run(["cache", "settings", "test", "--set", "ttl=60"], get=proxy(), post=post)
    assert r.exit_code == 2 and "Only Redis cache type" in r.output


def test_settings_test_dry_run_sends_nothing():
    post = mock.Mock(return_value=cache_test_ok())
    r = run(["--json", "--dry-run", "cache", "settings", "test", "--set", "ttl=60"], post=post)
    assert r.exit_code == 0 and post.call_count == 0
    body = json.loads(r.output)["body"]
    assert body["cache_settings"]["ttl"] == 60.0 and body["cache_settings"]["host"] == "redis.litellm.svc"


def test_settings_test_refusals_before_the_proxy():
    post = mock.Mock()
    for args in (
        ["cache", "settings", "test"],
        ["cache", "settings", "test", "--set", "ttl"],
        ["cache", "settings", "test", "--set", "password=hunter2"],
    ):
        r = run(args, post=post)
        assert r.exit_code == 1 and post.call_count == 0


# settings update


def test_settings_update_posts_the_merged_body():
    post = mock.Mock(side_effect=lambda c, p, body=None, **k: settings_updated(body))
    r = run(["--json", "cache", "settings", "update", "--set", "ttl=60"], get=proxy(), post=post)
    assert r.exit_code == 0
    path, body = post.call_args.args[1:]
    assert path == "/cache/settings"
    assert body["cache_settings"]["ttl"] == 60.0
    assert body["cache_settings"]["password"] == ca.REDACTED
    assert "redis_type" not in body["cache_settings"]  # UI-only, never re-sent
    out = json.loads(r.output)
    assert out["changed"] == {"ttl": 60.0}
    assert out["message"] == "Cache settings updated successfully"
    assert out["settings"]["password"] == ca.REDACTED
    no_secret(r.output)


def test_settings_update_dry_run_prints_the_exact_body():
    post = mock.Mock()
    r = run(["--json", "--dry-run", "cache", "settings", "update", "--set", "ssl=true"], post=post)
    assert r.exit_code == 0 and post.call_count == 0
    body = json.loads(r.output)["body"]
    assert body["cache_settings"]["ssl"] is True
    assert body["cache_settings"]["host"] == "redis.litellm.svc"


def test_settings_update_warns_when_a_target_change_drops_the_credential():
    post = mock.Mock(side_effect=lambda c, p, body=None, **k: settings_updated(body))
    r = run(["cache", "settings", "update", "--set", "host=r2"], get=proxy(), post=post)
    assert r.exit_code == 0
    assert "DROP the stored password" in r.output and "os.environ/NAME" in r.output
    r = run(["--json", "cache", "settings", "update", "--set", "host=r2"], get=proxy(), post=post)
    assert json.loads(r.output)["dropped_credentials"] == ["password"]
    no_secret(r.output)
    # supplying the credential with the new target silences it
    r = run(
        ["cache", "settings", "update", "--set", "host=r2", "--set", "password=os.environ/NEW"],
        get=proxy(),
        post=post,
    )
    assert r.exit_code == 0 and "DROP" not in r.output


def test_settings_update_gates_a_save_over_a_config_owned_cache():
    """Empty current_values: the cache comes from config.yaml, and a stored row
    beats the config at every restart — so the save needs --yes."""
    empty = settings_response({}, redacted=False)
    empty["current_values"] = {}
    post = mock.Mock()
    r = run(["cache", "settings", "update", "--set", "ttl=60"], get=proxy(settings=empty), post=post)
    assert r.exit_code == 1 and "config.yaml" in r.output and post.call_count == 0
    r = run(
        ["--json", "--dry-run", "cache", "settings", "update", "--set", "ttl=60", "--yes"],
        get=proxy(settings=empty),
    )
    body = json.loads(r.output)["body"]
    assert body["cache_settings"] == {"ttl": 60.0}  # nothing else to carry
    r = run(
        ["--json", "cache", "settings", "update", "--set", "ttl=60", "--yes"],
        get=proxy(settings=empty),
        post=post,
    )
    assert r.exit_code == 0 and post.call_count == 1


def test_settings_update_surfaces_store_model_and_db_500s():
    err = be.ApiError(500, store_model_required_text(), "http://p/cache/settings")
    post = mock.Mock(side_effect=err)
    r = run(["cache", "settings", "update", "--set", "ttl=60"], get=proxy(), post=post)
    assert r.exit_code == 1 and "STORE_MODEL_IN_DB" in r.output


# delete / flushall


def test_cache_delete_dry_run_then_yes():
    post = mock.Mock(return_value=cache_delete_ok())
    r = run(["--json", "--dry-run", "cache", "delete", "litellm_proxy:abc", "litellm_proxy:def"], post=post)
    assert r.exit_code == 0 and post.call_count == 0
    assert json.loads(r.output)["body"] == {"keys": ["litellm_proxy:abc", "litellm_proxy:def"]}
    r = run(["cache", "delete", "litellm_proxy:abc"], post=post)
    assert r.exit_code == 1 and "--yes" in r.output and post.call_count == 0
    r = run(["--json", "cache", "delete", "litellm_proxy:abc", "--yes"], post=post)
    assert r.exit_code == 0
    assert post.call_args.args[1:] == ("/cache/delete", {"keys": ["litellm_proxy:abc"]})
    assert json.loads(r.output) == {"deleted": ["litellm_proxy:abc"], "status": "success"}


def test_cache_delete_requires_a_key():
    post = mock.Mock()
    r = run(["cache", "delete", " "], post=post)
    assert r.exit_code == 1 and "at least one cache key" in r.output and post.call_count == 0


def test_cache_delete_failure_is_rewrapped_500():
    err = be.ApiError(
        500,
        cache_delete_failed_text(
            "500: Cache type local does not support deleting a key. only `redis` is supported"
        ),
        "http://p",
    )
    r = run(["cache", "delete", "k", "--yes"], post=mock.Mock(side_effect=err))
    assert r.exit_code == 1 and "Cache Delete Failed" in r.output


def test_flushall_dry_run_then_yes():
    post = mock.Mock(return_value={"status": "success"})
    r = run(["--json", "--dry-run", "cache", "flushall"], post=post)
    assert (
        r.exit_code == 0 and post.call_count == 0 and json.loads(r.output)["would"] == "POST /cache/flushall"
    )
    r = run(["cache", "flushall"], post=post)
    assert r.exit_code == 1 and "--yes" in r.output and post.call_count == 0
    r = run(["--json", "cache", "flushall", "--yes"], post=post)
    assert r.exit_code == 0
    assert post.call_args.args[1] == "/cache/flushall"
    assert json.loads(r.output) == {"status": "success", "flushed": True}


def test_flushall_failure_is_a_wrapped_503():
    err = be.ApiError(
        503, service_unhealthy_text("500: Cache type local does not support flushing"), "http://p"
    )
    r = run(["cache", "flushall", "--yes"], post=mock.Mock(side_effect=err))
    assert r.exit_code == 1 and "Service Unhealthy" in r.output
    no_cache = be.ApiError(
        503, service_unhealthy_text("503: Cache not initialized. litellm.cache is None"), "http://p"
    )
    r = run(["cache", "flushall", "--yes"], post=mock.Mock(side_effect=no_cache))
    assert r.exit_code == 1 and "Cache not initialized" in r.output


# ── workflows ──────────────────────────────────────────────────────────────


def test_workflow_stale_response_invalidation():
    """A client keeps getting a stale cached answer: check the cache is alive,
    see what it holds, drop the one key the response named."""
    post = mock.Mock(return_value=cache_delete_ok())
    get = proxy()
    r = run(["cache", "ping"], get=get)
    assert r.exit_code == 0 and "healthy" in r.output
    r = run(["cache", "info"], get=get)
    assert r.exit_code == 0 and "4330" in r.output
    r = run(["cache", "settings"], get=get)
    assert r.exit_code == 0 and "redis.litellm.svc" in r.output
    r = run(["--dry-run", "cache", "delete", "litellm_proxy:stale"], get=get, post=post)
    assert r.exit_code == 0 and post.call_count == 0
    r = run(["cache", "delete", "litellm_proxy:stale", "--yes"], get=get, post=post)
    assert r.exit_code == 0 and post.call_args.args[1] == "/cache/delete"


def test_workflow_repoint_the_cache():
    """Move the cache to a new Redis: test the credentials against it first,
    then save — and see the anti-replay warning when the credential is missing."""
    get = proxy()
    post = mock.Mock(
        side_effect=lambda c, p, body=None, **k: (
            settings_updated(body) if p == "/cache/settings" else cache_test_ok()
        )
    )
    r = run(
        ["cache", "settings", "test", "--set", "host=r2", "--set", "password=os.environ/R2_PASSWORD"],
        get=get,
        post=post,
    )
    assert r.exit_code == 0 and "DROP" not in r.output
    r = run(
        ["cache", "settings", "update", "--set", "host=r2", "--set", "password=os.environ/R2_PASSWORD"],
        get=get,
        post=post,
    )
    assert r.exit_code == 0 and "DROP" not in r.output
    body = post.call_args.args[2]
    assert body["cache_settings"]["host"] == "r2"
    assert body["cache_settings"]["password"] == "os.environ/R2_PASSWORD"
    r = run(["cache", "settings", "update", "--set", "host=r3"], get=get, post=post)
    assert r.exit_code == 0 and "DROP the stored password" in r.output


def test_workflow_flushall_after_a_prompt_change():
    post = mock.Mock(return_value={"status": "success"})
    r = run(["--dry-run", "cache", "flushall"])
    assert r.exit_code == 0 and post.call_count == 0
    r = run(["cache", "flushall", "--yes"], post=post)
    assert r.exit_code == 0 and post.call_args.args[1] == "/cache/flushall"


# ── the installed command ──────────────────────────────────────────────────


def _resolve_cli(name):
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
    module = name.replace("cli-anything-", "cli_anything.") + "." + name.split("-")[-1] + "_cli"
    print(f"[_resolve_cli] Falling back to: {sys.executable} -m {module}")
    return [sys.executable, "-m", module]


class TestCacheSubprocess:
    CLI_BASE = _resolve_cli("cli-anything-litellm")

    def _run(self, args):
        import os
        import subprocess

        env = {**os.environ, "LITELLM_URL": "http://127.0.0.1:9", "LITELLM_API_KEY": "sk-test"}
        return subprocess.run(self.CLI_BASE + args, capture_output=True, text=True, env=env, timeout=60)

    def test_help_lists_the_new_verbs(self):
        r = self._run(["cache", "--help"])
        assert r.returncode == 0
        for verb in ("ping", "info", "delete", "flushall", "settings"):
            assert verb in r.stdout
        r = self._run(["cache", "settings", "--help"])
        assert r.returncode == 0 and "show" in r.stdout and "test" in r.stdout and "update" in r.stdout

    def test_unreachable_proxy_fails_loudly(self):
        r = self._run(["--json", "cache", "ping"])
        assert r.returncode == 1 and "127.0.0.1:9" in r.stderr

    def test_literal_secret_refused_without_a_proxy(self):
        r = self._run(["cache", "settings", "update", "--set", "password=hunter2"])
        assert r.returncode == 1 and "os.environ/NAME" in r.stderr


# ── settings vs the live cache (house proxy, 2026-10-25) ─────────────────────
# config.yaml points the cache at one Redis (cache_params.host) while REDIS_*
# names the usage store; GET /cache/settings reports the usage store.


def _usage_store_settings():
    return settings_response({"host": "litellm-redis.litellm.svc"})


def test_live_target_reads_redis_kwargs():
    assert ca.live_target(ca.ping_row(ping_redis())) == {"host": "redis.litellm.svc", "port": "6379"}
    assert ca.live_target(ca.ping_row(ping_local())) == {}


def test_live_target_ignores_the_string_none_the_live_proxy_sends():
    """Captured from the house proxy (v1.106.3, 2026-10-25): health_check_cache_params
    carries unset host/port/namespace as the string "None", not null."""
    ping = ping_redis()
    ping["health_check_cache_params"].update(host="None", port="None", namespace="None")
    assert ca.live_target(ca.ping_row(ping)) == {"host": "redis.litellm.svc", "port": "6379"}


def test_target_mismatch_only_when_both_sides_name_a_host():
    ping = ca.ping_row(ping_redis())
    assert ca.target_mismatch({"host": "redis.litellm.svc", "port": "6379"}, ping) is None
    assert "litellm-redis.litellm.svc" in ca.target_mismatch({"host": "litellm-redis.litellm.svc"}, ping)
    assert ca.target_mismatch({"host": "redis.litellm.svc", "port": "6380"}, ping)
    assert ca.target_mismatch({"url": ca.REDACTED}, ping) is None
    assert ca.target_mismatch({"host": "x"}, ca.ping_row(ping_local())) is None


def test_settings_show_warns_when_the_settings_are_not_the_live_cache():
    r = run(["cache", "settings"], get=proxy(settings=_usage_store_settings()))
    assert r.exit_code == 0 and "the live response cache is redis.litellm.svc" in r.output
    out = json.loads(run(["--json", "cache", "settings"], get=proxy(settings=_usage_store_settings())).output)
    assert out["live_target"]["host"] == "redis.litellm.svc" and out["target_mismatch"]
    out = json.loads(run(["--json", "cache", "settings"], get=proxy()).output)
    assert out["target_mismatch"] is None


def test_settings_update_refuses_to_move_the_cache_onto_another_redis():
    post = mock.Mock()
    r = run(
        ["cache", "settings", "update", "--set", "ttl=60"],
        get=proxy(settings=_usage_store_settings()),
        post=post,
    )
    assert r.exit_code == 1 and "live response cache" in r.output and post.call_count == 0
    # naming a host in --set is a deliberate choice of target, so it is not refused
    r = run(
        [
            "--json",
            "--dry-run",
            "cache",
            "settings",
            "update",
            "--set",
            "ttl=60",
            "--set",
            "host=redis.litellm.svc",
        ],
        get=proxy(settings=_usage_store_settings()),
    )
    assert r.exit_code == 0, r.output
    # --yes moves it deliberately
    post = mock.Mock(side_effect=lambda c, p, body=None, **k: settings_updated(body))
    r = run(
        ["--json", "cache", "settings", "update", "--set", "ttl=60", "--yes"],
        get=proxy(settings=_usage_store_settings()),
        post=post,
    )
    assert r.exit_code == 0 and post.call_count == 1


def test_settings_update_without_a_live_cache_skips_the_check():
    post = mock.Mock(side_effect=lambda c, p, body=None, **k: settings_updated(body))
    r = run(
        ["--json", "cache", "settings", "update", "--set", "ttl=60"],
        get=proxy(
            ping=be.ApiError(503, "Service Unhealthy", "/cache/ping"), settings=_usage_store_settings()
        ),
        post=post,
    )
    assert r.exit_code == 0 and post.call_count == 1
