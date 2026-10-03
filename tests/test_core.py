"""Unit tests for the DB-model management core (models add / models delete)."""

from __future__ import annotations

import pytest

from cli_anything.litellm.core import models as m

from test_harness import dep


# ── new_deployment ────────────────────────────────────────────────────────


def test_new_deployment_minimal_body():
    out = m.new_deployment("qwen", "hosted_vllm/qwen")
    assert out == {"model_name": "qwen", "litellm_params": {"model": "hosted_vllm/qwen"}}


def test_new_deployment_with_params():
    out = m.new_deployment("qwen", "hosted_vllm/qwen", api_base="http://n1:8000/v1", api_key="os.environ/K", mode="chat", max_input_tokens=131072)
    assert out == {
        "model_name": "qwen",
        "litellm_params": {"model": "hosted_vllm/qwen", "api_base": "http://n1:8000/v1", "api_key": "os.environ/K"},
        "model_info": {"mode": "chat", "max_input_tokens": 131072},
    }


@pytest.mark.parametrize("bad", [("", "openai/gpt-4o"), ("qwen", ""), (None, None)])
def test_new_deployment_requires_name_and_model(bad):
    with pytest.raises(ValueError, match="--name.*--model|--model.*--name"):
        m.new_deployment(*bad)


# ── resolve_model_target ──────────────────────────────────────────────────


def test_resolve_target_by_id_names_the_deployment():
    t = m.resolve_model_target([dep(1), dep(2, db=True)], "node-2")
    assert t["by"] == "id" and t["matches"] == ["node-2"] and t["db_model"] is True


def test_resolve_target_by_name_and_flags_db_replica():
    t = m.resolve_model_target([dep(1), dep(2, db=True)], "qwen")
    assert t["by"] == "model_name" and t["matches"] == ["qwen"] and t["db_model"] is True


def test_resolve_target_prefers_id_and_reports_non_db():
    t = m.resolve_model_target([dep(1)], "node-1")
    assert t["by"] == "id" and t["db_model"] is False


def test_resolve_target_unknown_is_none():
    assert m.resolve_model_target([dep(1)], "nope") is None
    assert m.resolve_model_target([], "") is None


# ── users ─────────────────────────────────────────────────────────────────

from cli_anything.litellm.core import users as u


def user(i, role="internal_user", teams=("eng",), email=None, spend=1.5):
    return {
        "user_id": f"u{i}",
        "user_email": email or f"u{i}@corp.io",
        "user_role": role,
        "teams": list(teams),
        "spend": spend,
        "max_budget": 10,
    }


def test_normalize_names_the_human_fields():
    row = u.normalize(user(1, teams=("eng", "ops"), spend=1.23456))
    assert row == {
        "user_id": "u1",
        "email": "u1@corp.io",
        "role": "internal_user",
        "teams": ["eng", "ops"],
        "max_budget": 10,
        "rpm_limit": None,
        "tpm_limit": None,
        "blocked": None,
        "spend": 1.2346,
        "models": None,
    }


def test_new_user_minimal_never_mints_a_key():
    out = u.new_user(user_id="svc-bot", email="svc@corp.io")
    assert out == {"user_id": "svc-bot", "user_email": "svc@corp.io", "auto_create_key": False}


def test_new_user_full_body():
    out = u.new_user(
        user_id="svc-bot", role="proxy_admin_viewer", teams="a, b", max_budget=5.0, rpm=10, tpm=1000
    )
    assert out == {
        "user_id": "svc-bot",
        "user_role": "proxy_admin_viewer",
        "teams": ["a", "b"],
        "max_budget": 5.0,
        "rpm_limit": 10,
        "tpm_limit": 1000,
        "auto_create_key": False,
    }


@pytest.mark.parametrize("bad", [("", ""), (None, None), (None, "")])
def test_new_user_requires_id_or_email(bad):
    with pytest.raises(ValueError, match="--user-id.*--email|--email.*--user-id"):
        u.new_user(*bad)


def test_new_user_rejects_unknown_role_before_the_proxy():
    with pytest.raises(ValueError, match="unknown --role 'root'.*LiteLLM roles"):
        u.new_user(user_id="x", role="root")


def test_update_user_targets_the_id_and_drops_auto_create_key():
    body = u.update_user(user_id="svc-bot", teams="a,b")
    assert body == {"user_id": "svc-bot", "teams": ["a", "b"]}


def test_update_user_requires_a_target():
    with pytest.raises(ValueError, match="identify the user"):
        u.update_user()


# ── resolve_user_target ───────────────────────────────────────────────────


def test_target_resolved_by_user_id_preferring_the_exact_match():
    t = u.resolve_user_target([user(1), user(2)], "u2")
    assert t["by"] == "user_id" and t["matches"] == ["u2"] and t["user"]["user_id"] == "u2"


def test_target_resolved_by_unique_email():
    t = u.resolve_user_target([user(1)], "u1@corp.io")
    assert t["by"] == "user_email" and t["matches"] == ["u1"]


def test_target_ambiguous_email_is_refused_not_guessed():
    assert u.resolve_user_target([user(1), user(2, email="same@corp.io"), user(3, email="same@corp.io")], "same@corp.io") is None


def test_target_unknown_or_empty_is_none():
    assert u.resolve_user_target([user(1)], "ghost") is None
    assert u.resolve_user_target([user(1)], "") is None
