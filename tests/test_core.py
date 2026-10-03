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
