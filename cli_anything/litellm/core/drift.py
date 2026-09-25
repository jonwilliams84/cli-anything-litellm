"""Compare what git says against what a running proxy is doing.

Two comparisons:

* ``config_vs_live`` — a config.yaml (the file you deploy) against one proxy.
  The trap it exists for: models added through the Admin UI or ``/model/new``
  live in the proxy's database, not the file, so a proxy can quietly serve
  deployments that no config in git describes (``db_model: true``).
* ``fleet_diff`` — several proxies that sit behind one VIP. They must be
  identical; if they are not, clients see different behaviour per request.
"""

from __future__ import annotations

import json
from typing import Any

from cli_anything.litellm.core import models as m

# router_settings keys whose live value is reported by /router/settings.
ROUTER_KEYS = (
    "routing_strategy",
    "num_retries",
    "timeout",
    "stream_timeout",
    "allowed_fails",
    "cooldown_time",
    "retry_after",
    "max_fallbacks",
    "optional_pre_call_checks",
    "deployment_affinity_ttl_seconds",
    "enable_pre_call_checks",
)


def _is_env_ref(v: Any) -> bool:
    return isinstance(v, str) and v.startswith("os.environ/")


def _is_masked(v: Any) -> bool:
    # /model/info masks any value whose KEY looks sensitive — including
    # innocuous ones like extra_body.thinking_token_budget ("****").
    return isinstance(v, str) and ((len(v) >= 3 and set(v) <= {"*"}) or v.startswith("****"))


def _unmask(cfg_v: Any, live_v: Any, path: str, masked: list[str]) -> Any:
    """Return live_v with masked leaves replaced by the config value (recorded in ``masked``)."""
    if _is_masked(live_v):
        masked.append(path)
        return cfg_v
    if isinstance(cfg_v, dict) and isinstance(live_v, dict):
        return {k: _unmask(cfg_v.get(k), v, f"{path}.{k}", masked) for k, v in live_v.items()}
    return live_v


def compare_deployments(cfg_deps: list[dict], live_deps: list[dict]) -> dict[str, Any]:
    cfg = {m.dep_id(d): d for d in cfg_deps if m.dep_id(d)}
    live = {m.dep_id(d): d for d in live_deps if m.dep_id(d)}
    only_cfg = sorted(set(cfg) - set(live))
    only_live = sorted(set(live) - set(cfg))
    changed = []
    masked: set[str] = set()
    for i in sorted(set(cfg) & set(live)):
        c = m.fingerprint(cfg[i])
        lv = m.fingerprint(live[i], info_keys_from=(cfg[i].get("model_info") or {}))
        # Live never echoes env refs back, and reports params the config never
        # set (proxy defaults). Compare only what the config pins literally.
        c["params"] = {k: v for k, v in c["params"].items() if not _is_env_ref(v)}
        lv["params"] = {k: v for k, v in lv["params"].items() if k in c["params"]}
        lv["params"] = _unmask(c["params"], lv["params"], "params", masked_here := [])
        masked.update(p.split(".", 1)[1] for p in masked_here)
        if c != lv:
            diffs = {}
            for part in ("model_name", "params", "info"):
                if c[part] != lv[part]:
                    if isinstance(c[part], dict):
                        for k in sorted(set(c[part]) | set(lv[part])):
                            if c[part].get(k) != lv[part].get(k):
                                diffs[f"{part}.{k}"] = {"config": c[part].get(k), "live": lv[part].get(k)}
                    else:
                        diffs[part] = {"config": c[part], "live": lv[part]}
            changed.append({"id": i, "diffs": diffs})
    unidentified = sum(1 for d in cfg_deps if not m.dep_id(d))
    return {
        "only_in_config": only_cfg,
        "only_live": [
            {"id": i, "model_name": live[i].get("model_name"), "db_model": m.is_db_model(live[i])}
            for i in only_live
        ],
        "changed": changed,
        "config_without_id": unidentified,
        # Masked by the proxy, so NOT verified — say so rather than call it in sync.
        "unverifiable_masked_params": sorted(masked),
    }


def compare_router(cfg_router: dict[str, Any], live_values: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for k, v in (cfg_router or {}).items():
        if k not in ROUTER_KEYS or _is_env_ref(v):
            continue
        lv = live_values.get(k)
        if json.dumps(v, sort_keys=True, default=str) != json.dumps(lv, sort_keys=True, default=str):
            # int vs float spellings of the same number are not drift
            try:
                if float(v) == float(lv):
                    continue
            except (TypeError, ValueError):
                pass
            out.append({"key": k, "config": v, "live": lv})
    return out


def _guardrail_view(g: dict[str, Any]) -> dict[str, Any]:
    p = g.get("litellm_params") or {}
    mode = p.get("mode")
    return {
        "mode": sorted(mode) if isinstance(mode, list) else mode,
        "default_on": p.get("default_on"),
        "guardrail": p.get("guardrail"),
    }


def compare_guardrails(cfg_g: list[dict], live_g: list[dict]) -> dict[str, Any]:
    c = {g["guardrail_name"]: _guardrail_view(g) for g in cfg_g or []}
    lv = {g["guardrail_name"]: _guardrail_view(g) for g in live_g or []}
    changed = []
    for n in sorted(set(c) & set(lv)):
        a, b = c[n], lv[n]
        diffs = {
            k: {"config": a[k], "live": b[k]}
            for k in a
            if a[k] is not None and b.get(k) is not None and a[k] != b[k]
        }
        if diffs:
            changed.append({"name": n, "diffs": diffs})
    return {
        "only_in_config": sorted(set(c) - set(lv)),
        "only_live": sorted(set(lv) - set(c)),
        "changed": changed,
    }


def config_vs_live(
    cfg: dict[str, Any], live_deps: list[dict], live_router: dict[str, Any], live_guardrails: list[dict]
) -> dict[str, Any]:
    dep = compare_deployments(cfg.get("model_list") or [], live_deps)
    router = compare_router(cfg.get("router_settings") or {}, live_router or {})
    guard = compare_guardrails(cfg.get("guardrails") or [], live_guardrails)
    n = (
        len(dep["only_in_config"])
        + len(dep["only_live"])
        + len(dep["changed"])
        + len(router)
        + len(guard["only_in_config"])
        + len(guard["only_live"])
        + len(guard["changed"])
    )
    return {"in_sync": n == 0, "differences": n, "deployments": dep, "router": router, "guardrails": guard}


_FLEET_LABELS = {
    "config": "reference",
    "live": "node",
    "only_in_config": "only_on_reference",
    "only_live": "only_on_node",
}


def _relabel(obj: Any) -> Any:
    """config/live wording is wrong when both sides are proxies."""
    if isinstance(obj, dict):
        return {_FLEET_LABELS.get(k, k): _relabel(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_relabel(v) for v in obj]
    return obj


def fleet_diff(snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compare proxies pairwise against the first one (the reference).

    ``snapshots``: url -> {"error"?, "version", "deployments", "router", "guardrails", "keys"}.
    """
    urls = list(snapshots)
    ref_url = next((u for u in urls if "error" not in snapshots[u]), None)
    results = []
    for u in urls:
        s = snapshots[u]
        if "error" in s:
            results.append({"url": u, "error": s["error"]})
            continue
        if u == ref_url:
            results.append({"url": u, "reference": True, "version": s.get("version")})
            continue
        r = snapshots[ref_url]
        dep = compare_deployments(r["deployments"], s["deployments"])
        router = compare_router({k: v for k, v in r["router"].items() if k in ROUTER_KEYS}, s["router"])
        guard = compare_guardrails(r["guardrails"], s["guardrails"])
        results.append(
            {
                "url": u,
                "version": s.get("version"),
                "version_matches": s.get("version") == r.get("version"),
                "deployments": dep,
                "router": router,
                "guardrails": guard,
                "key_count": s.get("keys"),
                "reference_key_count": r.get("keys"),
            }
        )
    same = all(
        "error" not in x
        and (
            x.get("reference")
            or (
                x["version_matches"]
                and not x["router"]
                and not any(x["deployments"][k] for k in ("only_in_config", "only_live", "changed"))
                and not any(x["guardrails"].values())
            )
        )
        for x in results
    )
    return {"identical": same and ref_url is not None, "reference": ref_url, "proxies": _relabel(results)}
