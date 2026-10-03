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


# ── teams ─────────────────────────────────────────────────────────────────

from cli_anything.litellm.core import teams as t


def team(i, alias=None, members=1, blocked=None, teams_alias=None):
    return {
        "team_id": f"t-{i}",
        "team_alias": teams_alias or alias or f"team-{i}",
        "members_with_roles": [{"user_id": f"u{j}"} for j in range(members)],
        "spend": 2.25,
        "max_budget": 50,
        "blocked": blocked,
    }


def test_team_normalize_names_the_human_fields():
    row = t.normalize(team(1, members=2, teams_alias="eng"))
    assert row == {
        "team_id": "t-1",
        "alias": "eng",
        "members": 2,
        "spend": 2.25,
        "max_budget": 50,
        "rpm_limit": None,
        "tpm_limit": None,
        "models": None,
        "blocked": None,
    }


def test_new_team_minimal_pinned_id():
    out = t.new_team("eng", "team-eng")
    assert out == {"team_alias": "eng", "team_id": "team-eng"}


def test_new_team_full_body_splits_models_and_members():
    out = t.new_team(
        "eng",
        models="qwen, embed",
        max_budget=50.0,
        rpm=100,
        tpm=20000,
        budget_duration="30d",
        members=("u1", "new@corp.io"),
        member_role="admin",
    )
    assert out == {
        "team_alias": "eng",
        "models": ["qwen", "embed"],
        "max_budget": 50.0,
        "rpm_limit": 100,
        "tpm_limit": 20000,
        "budget_duration": "30d",
        "members_with_roles": [
            {"user_id": "u1", "team_member_role": "admin"},
            {"user_email": "new@corp.io", "team_member_role": "admin"},
        ],
    }


def test_new_team_requires_an_alias():
    with pytest.raises(ValueError, match="--alias"):
        t.new_team(None)


def test_new_team_rejects_unknown_member_role_before_the_proxy():
    with pytest.raises(ValueError, match="unknown --member-role 'owner'.*admin, user"):
        t.new_team("eng", members=("u1",), member_role="owner")


def test_update_team_targets_the_id_and_omits_unset():
    out = t.update_team("t-1", alias="eng", models="qwen, embed", max_budget=20.0)
    assert out == {"team_id": "t-1", "team_alias": "eng", "models": ["qwen", "embed"], "max_budget": 20.0}


def test_update_team_requires_a_target_and_something_to_change():
    with pytest.raises(ValueError, match="identify the team"):
        t.update_team("")
    with pytest.raises(ValueError, match="nothing to update"):
        t.update_team("t-1")


def test_member_entry_by_name_and_email():
    assert t.member_entry("svc-bot") == {"user_id": "svc-bot"}
    assert t.member_entry("svc@corp.io", "admin") == {"user_email": "svc@corp.io", "team_member_role": "admin"}
    with pytest.raises(ValueError, match="user name or email"):
        t.member_entry("")


# ── resolve_team_target ───────────────────────────────────────────────────


def test_team_target_resolved_by_id_preferring_the_exact_match():
    # 't-1' is also a team_alias elsewhere, but an exact team_id wins.
    res = t.resolve_team_target([team(1), team(2, teams_alias="t-1")], "t-1")
    assert res["by"] == "team_id" and res["matches"] == ["t-1"] and res["team"]["team_id"] == "t-1"


def test_team_target_resolved_by_unique_alias_names_the_team_id():
    res = t.resolve_team_target([team(1), team(2)], "team-2")
    assert res["by"] == "team_alias" and res["matches"] == ["t-2"] and res["team"]["team_id"] == "t-2"


def test_team_target_ambiguous_alias_is_refused_not_guessed():
    assert t.resolve_team_target([team(1, teams_alias="dup"), team(2, teams_alias="dup")], "dup") is None


def test_team_target_unknown_or_empty_is_none():
    assert t.resolve_team_target([team(1)], "ghost") is None
    assert t.resolve_team_target([team(1)], "") is None


# ── budgets ───────────────────────────────────────────────────────────────

from cli_anything.litellm.core import budgets as b


def budget_row(i, **kw):
    return {
        "budget_id": f"bud-{i}",
        "max_budget": 100.0,
        "soft_budget": None,
        "tpm_limit": 20000,
        "rpm_limit": 60,
        "max_parallel_requests": None,
        "budget_duration": "30d",
        "model_max_budget": None,
        "created_at": "2026-10-02T00:00:00",
        **kw,
    }


def test_as_rows_tolerates_list_and_envelopes():
    rows = [budget_row(1)]
    assert b.as_rows(rows) == rows
    assert b.as_rows({"data": rows}) == rows
    assert b.as_rows({"budgets": rows}) == rows
    assert b.as_rows({}) == [] and b.as_rows(None) == []


def test_normalize_keeps_the_limit_fields():
    row = b.normalize(budget_row(1, model_max_budget={"gpt-4o": 0.01}))
    assert row == {
        "budget_id": "bud-1",
        "max_budget": 100.0,
        "soft_budget": None,
        "budget_duration": "30d",
        "tpm_limit": 20000,
        "rpm_limit": 60,
        "max_parallel_requests": None,
        "model_max_budget": {"gpt-4o": 0.01},
    }


def test_new_budget_minimal_body_needs_something_to_cap():
    assert b.new_budget("bud-1", max_budget=100) == {"budget_id": "bud-1", "max_budget": 100}
    with pytest.raises(ValueError, match="something to cap"):
        b.new_budget("bud-1")


def test_new_budget_full_body_including_per_model_caps():
    out = b.new_budget(
        "bud-1", max_budget=100, soft_budget=90, rpm=60, tpm=20000, parallel=5,
        duration="30d", model_max_budget='{"gpt-4o": 0.01, "qwen": 0.05}',
    )
    assert out["budget_id"] == "bud-1"
    assert out["soft_budget"] == 90 and out["max_parallel_requests"] == 5
    assert out["budget_duration"] == "30d"
    assert out["model_max_budget"] == {"gpt-4o": 0.01, "qwen": 0.05}


def test_new_budget_accepts_a_dict_and_rejects_bad_json():
    assert b.new_budget(max_budget=1, model_max_budget={"qwen": 0.5}) == {
        "max_budget": 1,
        "model_max_budget": {"qwen": 0.5},
    }
    with pytest.raises(ValueError, match="--model-max-budget"):
        b.new_budget(max_budget=1, model_max_budget="not json")
    with pytest.raises(ValueError, match="--model-max-budget"):
        b.new_budget(model_max_budget='["not","an","object"]')


def test_update_budget_targets_the_id_and_omits_unset():
    out = b.update_budget("bud-1", max_budget=200.0, duration="7d")
    assert out == {"budget_id": "bud-1", "max_budget": 200.0, "budget_duration": "7d"}


def test_update_budget_requires_id_and_something_to_change():
    with pytest.raises(ValueError, match="identify the budget"):
        b.update_budget("", max_budget=1)
    with pytest.raises(ValueError, match="something to cap"):
        b.update_budget("bud-1")


def test_resolve_budget_target_is_an_exact_id_check():
    res = b.resolve_budget_target([budget_row(1), budget_row(2)], "bud-2")
    assert res["by"] == "budget_id" and res["matches"] == ["bud-2"] and res["budget"]["budget_id"] == "bud-2"
    assert b.resolve_budget_target([budget_row(1)], "ghost") is None
    assert b.resolve_budget_target([budget_row(1)], "") is None


def test_budget_delete_body_uses_id_not_budget_id():
    # /budget/delete is the one endpoint of the family that says `id`.
    assert b.DELETE_KEY == "id"
