"""Deployment normalisation shared by `models`, `drift`, `lint` and `fleet`.

A *deployment* is one ``model_list`` entry (one backend replica). Several
deployments share a ``model_name`` and form one load-balanced group. We reduce
each to a comparable fingerprint so the same entry read from a config file and
from ``/model/info`` compares equal.
"""

from __future__ import annotations

import json
from typing import Any

# litellm_params that are secrets, transport noise, or proxy-injected defaults
# which /model/info reports but a config file never sets.
PARAM_IGNORE = {
    "api_key",
    "mock_response",
    "use_in_pass_through",
    "use_litellm_proxy",
    "use_xai_oauth",
    "allow_client_keepalive_override",
    "merge_reasoning_content_in_choices",
}
# model_info fields worth comparing. /model/info returns ~60 derived fields
# (litellm's own cost map); only the ones an operator sets are compared.
INFO_KEYS = (
    "mode",
    "max_input_tokens",
    "max_output_tokens",
    "max_tokens",
    "input_cost_per_token",
    "output_cost_per_token",
    "output_vector_size",
    "supports_function_calling",
    "supports_tool_choice",
    "supports_vision",
    "supports_prompt_caching",
    "supports_response_schema",
    "supports_reasoning",
    "tier",
)


def _canon(v: Any) -> Any:
    """JSON-round-trip so dict ordering and int/float spellings compare equal."""
    return json.loads(json.dumps(v, sort_keys=True, default=str))


def fingerprint(dep: dict[str, Any], *, info_keys_from: dict | None = None) -> dict[str, Any]:
    """Comparable view of one deployment.

    ``info_keys_from``: when comparing live against config, only compare the
    model_info keys the CONFIG actually sets — live fills in defaults for the rest.
    """
    params = {
        k: v for k, v in (dep.get("litellm_params") or {}).items() if k not in PARAM_IGNORE and v is not None
    }
    info = dep.get("model_info") or {}
    keys = [k for k in INFO_KEYS if k in (info_keys_from if info_keys_from is not None else info)]
    return _canon(
        {
            "model_name": dep.get("model_name"),
            "params": params,
            "info": {k: info.get(k) for k in keys},
        }
    )


def dep_id(dep: dict[str, Any]) -> str | None:
    return (dep.get("model_info") or {}).get("id")


def is_db_model(dep: dict[str, Any]) -> bool:
    """True when the deployment lives in the proxy's DB (added via UI/API), not config."""
    return bool((dep.get("model_info") or {}).get("db_model"))


def group(deps: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for d in deps:
        out.setdefault(d.get("model_name") or "?", []).append(d)
    return out


def summarize(deps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per model group: replicas, backends, context, cost, variants."""
    rows = []
    for name, ds in sorted(group(deps).items()):
        info = [d.get("model_info") or {} for d in ds]
        params = [d.get("litellm_params") or {} for d in ds]
        rows.append(
            {
                "model_name": name,
                "replicas": len(ds),
                "db_replicas": sum(1 for d in ds if is_db_model(d)),
                "mode": sorted({i.get("mode") or "chat" for i in info}),
                "max_input_tokens": sorted({i.get("max_input_tokens") for i in info}, key=str),
                "input_cost_per_token": sorted({i.get("input_cost_per_token") for i in info}, key=str),
                "backends": sorted({p.get("api_base") or p.get("model") for p in params}, key=str),
                "extra_body_variants": len({json.dumps(p.get("extra_body"), sort_keys=True) for p in params}),
                "ids": [dep_id(d) for d in ds],
            }
        )
    return rows


def replica_inconsistencies(deps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deployments in one group that differ in anything but their backend.

    Replicas of a group are meant to be interchangeable; one with a different
    context limit, cost or extra_body means the router hands the same request
    to backends that behave differently depending on which one it picks.
    """
    issues = []
    for name, ds in group(deps).items():
        if len(ds) < 2:
            continue

        def shape(d):
            f = fingerprint(d)
            f["params"].pop("api_base", None)
            return json.dumps(f, sort_keys=True)

        shapes: dict[str, list[str]] = {}
        for d in ds:
            shapes.setdefault(shape(d), []).append(dep_id(d) or "?")
        if len(shapes) > 1:
            variants = sorted(shapes.values(), key=len, reverse=True)
            issues.append({"model_name": name, "majority": variants[0], "odd_ones": variants[1:]})
    return issues


def new_deployment(
    name: str,
    model: str,
    *,
    api_base: str | None = None,
    api_key: str | None = None,
    mode: str | None = None,
    max_input_tokens: int | None = None,
) -> dict[str, Any]:
    """Body for ``POST /model/new`` — a DB-only deployment, as the Admin UI would create it.

    ``model`` is the LiteLLM provider string (``hosted_vllm/qwen``, ``openai/gpt-4o``,
    …). Pass ``os.environ/NAME`` in ``api_key`` so the secret is resolved by the
    proxy, never stored in its database. ``mode`` is a model capability class
    (chat, embedding, …) and defaults server-side to chat.

    Raises ``ValueError`` (surfaced as a Click error) when ``name`` or ``model``
    is missing.
    """
    if not name or not model:
        raise ValueError("models add needs both --name (model group) and --model (litellm provider string)")
    lp: dict[str, Any] = {"model": model}
    if api_base:
        lp["api_base"] = api_base
    if api_key:
        lp["api_key"] = api_key
    mi: dict[str, Any] = {}
    if mode:
        mi["mode"] = mode
    if max_input_tokens is not None:
        mi["max_input_tokens"] = max_input_tokens
    out: dict[str, Any] = {
        "model_name": name,
        "litellm_params": lp,
        "model_info": mi,
    }
    if not mi:
        out.pop("model_info")
    return out


def resolve_model_target(deps: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """What ``models delete`` would remove for ``ref`` (a deployment id or a model group name).

    An exact ``model_info.id`` match deletes one replica; a ``model_name`` match
    deletes every replica of the group — including ones the config still
    describes (they come back on the next config redeploy; DB-only ones do not).
    Returns ``{"by": "id"|"model_name", "matches": [...]}`` or ``None`` when no
    live deployment matches.
    """
    if ref and ref in {dep_id(d) for d in deps}:
        return {
            "by": "id",
            "matches": [ref],
            "db_model": is_db_model(next(d for d in deps if dep_id(d) == ref)),
        }
    if ref and ref in {d.get("model_name") for d in deps}:
        return {
            "by": "model_name",
            "matches": [ref],
            "db_model": any(is_db_model(d) for d in deps if d.get("model_name") == ref),
        }
    return None
