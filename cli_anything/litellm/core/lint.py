"""Static checks on a LiteLLM config.yaml, plus an optional site policy.

Built-in rules are generic LiteLLM hygiene. A *policy file* adds the rules a
particular estate has decided on (routing strategy, advertised context, …) so
an agent tuning the proxy cannot quietly undo a recorded decision::

    rules:
      - id: routing-least-busy
        path: router_settings.routing_strategy
        equals: least-busy
        why: ADR-0018 — load-aware routing
      - id: affinity
        path: router_settings.optional_pre_call_checks
        contains: session_affinity
      - id: advertised-context
        each_deployment: {mode: chat}
        path: model_info.max_input_tokens
        equals: 131072
      - id: master-key-from-env
        path: general_settings.master_key
        startswith: os.environ/

      - id: affinity-bridge-loaded
        path: guardrails
        contains: {guardrail_name: session-affinity-bridge}

Operators: ``equals``, ``contains`` (a list item, a substring, or — given a
mapping — any list item whose fields match it), ``startswith``, ``exists``
(true/false), ``in`` (list of allowed values), ``min`` / ``max`` (numbers).
Each rule may set ``level`` (error | warn | info, default error) and ``why``.
"""

from __future__ import annotations

import re
from typing import Any

from cli_anything.litellm.core import models as m

SECRET_FIELDS = {"api_key", "master_key", "database_url", "redis_password", "password", "api_secret"}
SECRET_LOOKING = re.compile(r"^(sk-[A-Za-z0-9_\-]{8,}|postgres(ql)?://[^:]+:[^@]+@)")

# Values that mean "this backend takes no key" rather than a leaked secret.
PLACEHOLDERS = {"", "none", "null", "dummy", "empty", "not-needed", "no-key", "sk-no-key", "x"}

_MISSING = object()


def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _walk(obj: Any, prefix: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def builtin(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(rule, level, msg, where=None):
        out.append({"rule": rule, "level": level, "message": msg, "where": where})

    for path, v in _walk(cfg):
        leaf = re.sub(r"\[\d+\]", "", path).split(".")[-1]
        if not isinstance(v, str) or v.startswith("os.environ/"):
            continue
        if leaf in SECRET_FIELDS and v.strip().lower() in PLACEHOLDERS:
            add(
                "placeholder-credential",
                "info",
                f"{leaf} is the placeholder {v!r}: the backend accepts unauthenticated requests — intended?",
                path,
            )
        elif leaf in SECRET_FIELDS or SECRET_LOOKING.match(v):
            add("literal-secret", "error", "credential written literally; use os.environ/NAME", path)

    deps = cfg.get("model_list") or []
    ids = [m.dep_id(d) for d in deps]
    seen: set[str] = set()
    for i in ids:
        if i and i in seen:
            add(
                "duplicate-id",
                "error",
                f"model_info.id {i!r} used twice — drift and per-deployment health cannot tell them apart",
                i,
            )
        seen.add(i or "")
    missing = sum(1 for i in ids if not i)
    if missing:
        add(
            "no-deployment-id",
            "warn",
            f"{missing} deployment(s) lack model_info.id; LiteLLM "
            "hashes one, so it changes whenever params change and drift cannot follow it",
        )

    for issue in m.replica_inconsistencies(deps):
        add(
            "replica-mismatch",
            "warn",
            f"replicas of {issue['model_name']!r} differ in more than their backend: "
            f"{issue['odd_ones']} vs {issue['majority'][:3]}…",
            issue["model_name"],
        )

    for d in deps:
        info = d.get("model_info") or {}
        if (info.get("mode") or "chat") == "chat" and "max_input_tokens" not in info:
            add(
                "no-context-limit",
                "warn",
                "chat deployment without max_input_tokens: clients "
                "cannot size prompts and pre-call checks cannot reject oversize ones",
                m.dep_id(d),
            )

    groups = m.group(deps)
    rs = cfg.get("router_settings") or {}
    strategy = rs.get("routing_strategy", "simple-shuffle")
    if any(len(v) > 1 for v in groups.values()) and strategy == "simple-shuffle":
        add(
            "load-unaware-routing",
            "info",
            "simple-shuffle ignores load; with replicas of "
            "uneven request cost, least-busy or latency-based-routing usually fits better",
        )
    return out


def _check(op: str, want: Any, got: Any) -> bool:
    if op == "exists":
        return (got is not _MISSING) == bool(want)
    if got is _MISSING:
        return False
    if op == "equals":
        return got == want
    if op == "contains":
        if isinstance(want, dict) and isinstance(got, list):
            # list of objects: any item whose fields include all of `want`
            return any(isinstance(i, dict) and all(i.get(k) == v for k, v in want.items()) for i in got)
        return isinstance(got, (list, str)) and want in got
    if op == "startswith":
        return isinstance(got, str) and got.startswith(want)
    if op == "in":
        return got in want
    if op in ("min", "max"):
        try:
            return float(got) >= float(want) if op == "min" else float(got) <= float(want)
        except (TypeError, ValueError):
            return False
    raise ValueError(f"unknown operator {op!r}")


OPS = ("equals", "contains", "startswith", "exists", "in", "min", "max")


def apply_policy(cfg: dict[str, Any], policy: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in policy.get("rules") or []:
        op = next((o for o in OPS if o in r), None)
        if op is None or "path" not in r:
            out.append(
                {
                    "rule": r.get("id", "?"),
                    "level": "error",
                    "message": f"bad policy rule (needs path + one of {OPS})",
                    "where": None,
                }
            )
            continue
        level = r.get("level", "error")
        why = f" ({r['why']})" if r.get("why") else ""
        if "each_deployment" in r:
            where = r["each_deployment"] or {}
            for d in cfg.get("model_list") or []:
                info = d.get("model_info") or {}
                if all((info.get(k) or ("chat" if k == "mode" else None)) == v for k, v in where.items()):
                    got = get_path(d, r["path"])
                    if not _check(op, r[op], got):
                        out.append(
                            {
                                "rule": r.get("id", r["path"]),
                                "level": level,
                                "message": f"{r['path']} is {_show(got)}, policy {op} {r[op]!r}{why}",
                                "where": m.dep_id(d),
                            }
                        )
        else:
            got = get_path(cfg, r["path"])
            if not _check(op, r[op], got):
                out.append(
                    {
                        "rule": r.get("id", r["path"]),
                        "level": level,
                        "message": f"{r['path']} is {_show(got)}, policy {op} {r[op]!r}{why}",
                        "where": None,
                    }
                )
    return out


def _show(v: Any) -> str:
    if v is _MISSING:
        return "unset"
    if isinstance(v, str) and SECRET_LOOKING.match(v):
        return repr(v[:5] + "…(masked)")  # never echo a credential into a lint report
    return repr(v)


def lint(cfg: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    findings = builtin(cfg) + (apply_policy(cfg, policy) if policy else [])
    worst = "ok"
    for lvl in ("info", "warn", "error"):
        if any(f["level"] == lvl for f in findings):
            worst = lvl
    return {"level": worst, "count": len(findings), "findings": findings}
