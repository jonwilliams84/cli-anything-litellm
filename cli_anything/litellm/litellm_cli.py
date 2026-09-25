"""cli-anything-litellm — administer a LiteLLM proxy from the shell.

Read-only by default. Every mutating verb supports --dry-run (prints the
request, sends nothing) and destructive ones need --yes when not on a TTY.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import click
import yaml

from cli_anything.litellm import __version__
from cli_anything.litellm.core import backend as be
from cli_anything.litellm.core import drift as drift_mod
from cli_anything.litellm.core import lint as lint_mod
from cli_anything.litellm.core import models as models_mod


# ── plumbing ────────────────────────────────────────────────────────────────


def _conn(ctx: click.Context, url: str | None = None) -> dict[str, Any]:
    o = ctx.obj
    return be.resolve_conn(url or o["url"], o["key"])


def _emit(ctx: click.Context, data: Any, human=None) -> None:
    if ctx.obj["json"] or human is None:
        click.echo(json.dumps(data, indent=2, default=str))
    elif callable(human):
        human(data)
    else:
        click.echo(human)


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except be.ApiError as e:
        hint = ""
        if e.status == 401:
            hint = (
                "  (wrong key — behind a VIP, a 401 on SOME requests usually means the "
                "gateways' master keys differ)"
            )
        raise click.ClickException(f"{e}{hint}") from e


def _dry(ctx: click.Context, method: str, path: str, body: Any = None) -> bool:
    if ctx.obj["dry_run"]:
        _emit(ctx, {"dry_run": True, "would": f"{method} {path}", "body": body})
        return True
    return False


def _confirm(ctx: click.Context, yes: bool, what: str) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise click.ClickException(f"refusing to {what} without --yes (no TTY to confirm)")
    click.confirm(f"{what}?", abort=True)


def _table(rows: list[dict], cols: list[tuple[str, str]]) -> None:
    if not rows:
        click.echo("  (none)")
        return
    cells = [[str(r.get(k, "")) if r.get(k) is not None else "" for k, _ in cols] for r in rows]
    widths = [max(len(h), *(len(c[i]) for c in cells)) for i, (_, h) in enumerate(cols)]
    click.echo("  ".join(h.ljust(w) for (_, h), w in zip(cols, widths, strict=True)))
    for c in cells:
        click.echo("  ".join(v.ljust(w) for v, w in zip(c, widths, strict=True)))


def _load_yaml(path: str) -> dict[str, Any]:
    try:
        return yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise click.ClickException(f"cannot read {path}: {e}") from e


# live readers shared by drift / fleet
def live_deployments(conn) -> list[dict]:
    return (be.get(conn, "/v1/model/info") or {}).get("data") or []


def live_router(conn) -> dict:
    return (be.get(conn, "/router/settings") or {}).get("current_values") or {}


def live_guardrails(conn) -> list[dict]:
    return (be.get(conn, "/guardrails/list") or {}).get("guardrails") or []


def readiness(conn) -> dict:
    """Readiness with version/cache/callbacks.

    From ~1.100 plain /health/readiness returns only status + db; the detail
    moved to the authenticated /health/readiness/details. Older proxies have
    no /details and put everything on /health/readiness.
    """
    try:
        return be.get(conn, "/health/readiness/details") or {}
    except be.ApiError as e:
        if e.status not in (404, 405):
            raise
    return be.get(conn, "/health/readiness", auth=False) or {}


def live_version(conn) -> str | None:
    try:
        return readiness(conn).get("litellm_version")
    except be.ApiError:
        return None


# ── root ────────────────────────────────────────────────────────────────────


@click.group()
@click.option("--url", envvar=None, default=None, help="Proxy base URL (else LITELLM_URL / saved).")
@click.option("--key", default=None, help="Admin/master key (else LITELLM_API_KEY / saved).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--dry-run", is_flag=True, help="Mutations print the request and send nothing.")
@click.version_option(__version__, prog_name="cli-anything-litellm")
@click.pass_context
def cli(ctx, url, key, as_json, dry_run):
    """Administer a LiteLLM proxy: models, routing, keys, spend, drift and policy."""
    ctx.obj = {"url": url, "key": key, "json": as_json, "dry_run": dry_run}


# ── config ──────────────────────────────────────────────────────────────────


@cli.group("config")
def config_grp():
    """Saved connection (~/.cli-anything/litellm/config.json, mode 600)."""


@config_grp.command("set")
@click.option("--url", required=True)
@click.option("--key", default=None, help="Stored in plaintext (0600). Prefer LITELLM_API_KEY.")
@click.option("--ca-bundle", default=None, type=click.Path(exists=True))
@click.option("--timeout", default=None, type=float)
@click.pass_context
def config_set(ctx, url, key, ca_bundle, timeout):
    conf = be.load_saved()
    conf["url"] = url.rstrip("/")
    for k, v in (("key", key), ("ca_bundle", ca_bundle), ("timeout", timeout)):
        if v is not None:
            conf[k] = v
    path = be.save(conf)
    _emit(ctx, {"saved": str(path), "url": conf["url"], "key_saved": "key" in conf})


@config_grp.command("show")
@click.pass_context
def config_show(ctx):
    c = _conn(ctx)
    k = c.get("key") or ""
    _emit(
        ctx,
        {
            "url": c["url"],
            "key": (k[:5] + "…" + k[-4:]) if k else None,
            "verify": c["verify"],
            "timeout": c["timeout"],
            "config_file": str(be.CONFIG_PATH),
        },
    )


@config_grp.command("test")
@click.pass_context
def config_test(ctx):
    """Reachability, auth and version in one call."""
    c = _conn(ctx)
    out: dict[str, Any] = {"url": c["url"]}
    try:
        out["liveliness"] = be.get(c, "/health/liveliness", auth=False)
        out["version"] = live_version(c)
        out["auth"] = "ok" if be.get(c, "/key/list", params={"size": 1}) is not None else "?"
    except be.ApiError as e:
        out["error"] = str(e)
    _emit(ctx, out)
    if "error" in out:
        ctx.exit(1)


# ── status / health ─────────────────────────────────────────────────────────


@cli.command("status")
@click.pass_context
def status(ctx):
    """Liveliness, readiness (DB, cache, version) and active callbacks."""
    c = _conn(ctx)
    ready = _call(readiness, c)
    out = {
        "url": c["url"],
        "liveliness": _call(be.get, c, "/health/liveliness", auth=False),
        "version": ready.get("litellm_version"),
        "db": ready.get("db"),
        "cache": ready.get("cache"),
        "callbacks": ready.get("success_callbacks"),
    }
    try:
        out["cache_ping"] = (be.get(c, "/cache/ping") or {}).get("status")
    except be.ApiError as e:
        out["cache_ping"] = f"error {e.status}"
    _emit(ctx, out, None)


@cli.command("health")
@click.option("--model", default=None, help="Only this model group.")
@click.pass_context
def health(ctx, model):
    """Per-deployment health (GET /health). Sends a real request to every backend."""
    c = _conn(ctx)
    res = _call(be.get, c, "/health", params={"model": model} if model else None) or {}

    def pick(e):
        return {
            "id": (e.get("model_info") or {}).get("id") or e.get("model_id"),
            "model": e.get("model"),
            "api_base": e.get("api_base"),
            "error": (str(e.get("error"))[:120] if e.get("error") else None),
        }

    data = {
        "healthy_count": res.get("healthy_count"),
        "unhealthy_count": res.get("unhealthy_count"),
        "unhealthy": [pick(e) for e in res.get("unhealthy_endpoints") or []],
        "healthy": [pick(e) for e in res.get("healthy_endpoints") or []],
    }

    def human(d):
        click.echo(f"  healthy {d['healthy_count']}  unhealthy {d['unhealthy_count']}")
        _table(d["unhealthy"], [("id", "UNHEALTHY"), ("api_base", "BACKEND"), ("error", "ERROR")])

    _emit(ctx, data, human)


# ── models / router / guardrails ────────────────────────────────────────────


@cli.group("models")
def models_grp():
    """Deployments and model groups."""


@models_grp.command("list")
@click.option("--deployments", is_flag=True, help="One row per replica instead of per group.")
@click.pass_context
def models_list(ctx, deployments):
    deps = _call(live_deployments, _conn(ctx))
    if deployments:
        rows = [
            {
                "model_name": d.get("model_name"),
                "id": models_mod.dep_id(d),
                "api_base": (d.get("litellm_params") or {}).get("api_base"),
                "max_input_tokens": (d.get("model_info") or {}).get("max_input_tokens"),
                "source": "db" if models_mod.is_db_model(d) else "config",
            }
            for d in deps
        ]
        _emit(
            ctx,
            rows,
            lambda r: _table(
                r,
                [
                    ("model_name", "MODEL"),
                    ("id", "ID"),
                    ("api_base", "BACKEND"),
                    ("max_input_tokens", "CTX"),
                    ("source", "SRC"),
                ],
            ),
        )
        return
    rows = models_mod.summarize(deps)
    _emit(
        ctx,
        rows,
        lambda r: _table(
            [
                dict(
                    x,
                    backends=len(x["backends"]),
                    mode=",".join(x["mode"]),
                    max_input_tokens=",".join(map(str, x["max_input_tokens"])),
                )
                for x in r
            ],
            [
                ("model_name", "MODEL"),
                ("replicas", "REPLICAS"),
                ("db_replicas", "FROM-DB"),
                ("backends", "BACKENDS"),
                ("mode", "MODE"),
                ("max_input_tokens", "CTX"),
            ],
        ),
    )


@models_grp.command("consistency")
@click.pass_context
def models_consistency(ctx):
    """Replicas of one group that differ in more than their backend."""
    issues = models_mod.replica_inconsistencies(_call(live_deployments, _conn(ctx)))
    _emit(ctx, {"consistent": not issues, "issues": issues})


@cli.group("router")
def router_grp():
    """Live router settings."""


@router_grp.command("show")
@click.option("--all", "show_all", is_flag=True, help="Every field, not just the ones that matter.")
@click.pass_context
def router_show(ctx, show_all):
    v = _call(live_router, _conn(ctx))
    if not show_all:
        v = {k: v.get(k) for k in drift_mod.ROUTER_KEYS if k in v}
    _emit(ctx, v)


@cli.group("guardrails")
def guardrails_grp():
    """Guardrails loaded on the proxy."""


@guardrails_grp.command("list")
@click.pass_context
def guardrails_list(ctx):
    g = _call(live_guardrails, _conn(ctx))
    rows = [
        {
            "name": x.get("guardrail_name"),
            **drift_mod._guardrail_view(x),
            "teams": (x.get("litellm_params") or {}).get("teams"),
        }
        for x in g
    ]
    _emit(
        ctx,
        rows,
        lambda r: _table(
            r,
            [
                ("name", "NAME"),
                ("mode", "MODE"),
                ("default_on", "DEFAULT"),
                ("teams", "TEAMS"),
                ("guardrail", "CLASS"),
            ],
        ),
    )


# ── keys ────────────────────────────────────────────────────────────────────

KEY_COLS = [
    ("key_alias", "ALIAS"),
    ("key_name", "KEY"),
    ("team_id", "TEAM"),
    ("user_id", "USER"),
    ("spend", "SPEND"),
    ("max_budget", "BUDGET"),
    ("expires", "EXPIRES"),
    ("blocked", "BLOCKED"),
]


def all_keys(conn, team: str | None = None) -> list[dict]:
    out, page = [], 1
    while True:
        params = {"page": page, "size": 100, "return_full_object": "true"}
        if team:
            params["team_id"] = team
        res = be.get(conn, "/key/list", params=params) or {}
        out += res.get("keys") or []
        if page >= (res.get("total_pages") or 1):
            return out
        page += 1


def _key_row(k: dict) -> dict:
    return {c: k.get(c) for c, _ in KEY_COLS} | {
        "spend": round(k.get("spend") or 0, 4),
        "models": k.get("models"),
        "rpm_limit": k.get("rpm_limit"),
        "tpm_limit": k.get("tpm_limit"),
        "token": k.get("token"),
    }


@cli.group("keys")
def keys_grp():
    """Virtual keys. (User onboarding end-to-end: the gateway's onboard.sh.)"""


@keys_grp.command("list")
@click.option("--team", default=None)
@click.pass_context
def keys_list(ctx, team):
    rows = [_key_row(k) for k in _call(all_keys, _conn(ctx), team)]
    _emit(ctx, rows, lambda r: _table(r, KEY_COLS))


@keys_grp.command("info")
@click.argument("key_or_alias")
@click.pass_context
def keys_info(ctx, key_or_alias):
    """By key (sk-…), hashed token, or alias."""
    c = _conn(ctx)
    if key_or_alias.startswith("sk-") or len(key_or_alias) == 64:
        res = _call(be.get, c, "/key/info", params={"key": key_or_alias})
        _emit(ctx, res)
        return
    hits = [k for k in _call(all_keys, c) if k.get("key_alias") == key_or_alias]
    if not hits:
        raise click.ClickException(f"no key with alias {key_or_alias!r}")
    _emit(ctx, hits[0] if len(hits) == 1 else hits)


def _resolve_token(conn, key_or_alias: str) -> str:
    if key_or_alias.startswith("sk-") or len(key_or_alias) == 64:
        return key_or_alias
    hits = [k for k in all_keys(conn) if k.get("key_alias") == key_or_alias]
    if len(hits) != 1:
        raise click.ClickException(f"alias {key_or_alias!r} matches {len(hits)} keys")
    return hits[0]["token"]


@keys_grp.command("generate")
@click.option("--alias", required=True)
@click.option("--team", default=None)
@click.option("--user", default=None)
@click.option("--models", default=None, help="Comma-separated model groups; default all.")
@click.option("--duration", default=None, help="e.g. 30d, 12h. Default: no expiry.")
@click.option("--max-budget", type=float, default=None)
@click.option("--rpm", type=int, default=None)
@click.option("--tpm", type=int, default=None)
@click.pass_context
def keys_generate(ctx, alias, team, user, models, duration, max_budget, rpm, tpm):
    body = {
        k: v
        for k, v in {
            "key_alias": alias,
            "team_id": team,
            "user_id": user,
            "models": models.split(",") if models else None,
            "duration": duration,
            "max_budget": max_budget,
            "rpm_limit": rpm,
            "tpm_limit": tpm,
        }.items()
        if v is not None
    }
    if _dry(ctx, "POST", "/key/generate", body):
        return
    res = _call(be.post, _conn(ctx), "/key/generate", body)
    _emit(ctx, res)


@keys_grp.command("update")
@click.argument("key_or_alias")
@click.option("--models", default=None)
@click.option("--max-budget", type=float, default=None)
@click.option("--rpm", type=int, default=None)
@click.option("--tpm", type=int, default=None)
@click.option("--duration", default=None)
@click.pass_context
def keys_update(ctx, key_or_alias, models, max_budget, rpm, tpm, duration):
    c = _conn(ctx)
    body = {
        k: v
        for k, v in {
            "models": models.split(",") if models else None,
            "max_budget": max_budget,
            "rpm_limit": rpm,
            "tpm_limit": tpm,
            "duration": duration,
        }.items()
        if v is not None
    }
    if not body:
        raise click.ClickException("nothing to update")
    body["key"] = _call(_resolve_token, c, key_or_alias)
    if _dry(ctx, "POST", "/key/update", body):
        return
    _emit(ctx, _call(be.post, c, "/key/update", body))


def _simple_key_action(name: str, path: str, destructive: bool):
    @keys_grp.command(name)
    @click.argument("key_or_alias")
    @click.option("--yes", is_flag=True)
    @click.pass_context
    def _cmd(ctx, key_or_alias, yes):
        c = _conn(ctx)
        token = _call(_resolve_token, c, key_or_alias)
        body = {"keys": [token]} if name == "delete" else {"key": token}
        if _dry(ctx, "POST", path, body):
            return
        if destructive:
            _confirm(ctx, yes, f"{name} key {key_or_alias}")
        _emit(ctx, _call(be.post, c, path, body))

    _cmd.__doc__ = {
        "block": "Block a key (reversible with unblock).",
        "unblock": "Unblock a key.",
        "delete": "Delete a key permanently.",
    }[name]
    return _cmd


_simple_key_action("block", "/key/block", True)
_simple_key_action("unblock", "/key/unblock", False)
_simple_key_action("delete", "/key/delete", True)


ROTATE_FIELDS = (
    "key_alias",
    "team_id",
    "user_id",
    "models",
    "max_budget",
    "budget_duration",
    "rpm_limit",
    "tpm_limit",
    "max_parallel_requests",
    "metadata",
    "tags",
    "allowed_routes",
    "guardrails",
)


@keys_grp.command("rotate")
@click.argument("key_or_alias")
@click.option("--duration", default=None, help="Lifetime of the new key (e.g. 90d). Default: none.")
@click.option("--yes", is_flag=True)
@click.pass_context
def keys_rotate(ctx, key_or_alias, duration, yes):
    """New secret with the same alias, team, models and limits; then delete the old one.

    /key/regenerate does this in one call but is an Enterprise feature, so on
    the open-source proxy it is three: rename the old key out of the way,
    generate the replacement under the original alias, delete the old key.
    The old secret stops working at the last step — hand the new one over first
    if the caller cannot tolerate a gap.
    """
    c = _conn(ctx)
    token = _call(_resolve_token, c, key_or_alias)
    info = _call(be.get, c, "/key/info", params={"key": token}) or {}
    old = info.get("info") or info
    alias = old.get("key_alias") or key_or_alias
    new_body = {k: old.get(k) for k in ROTATE_FIELDS if old.get(k) not in (None, [], {})}
    new_body["key_alias"] = alias
    if duration:
        new_body["duration"] = duration
    steps = [
        {"call": "POST /key/update", "body": {"key": token, "key_alias": f"{alias}-rotating"}},
        {"call": "POST /key/generate", "body": new_body},
        {"call": "POST /key/delete", "body": {"keys": [token]}},
    ]
    if ctx.obj["dry_run"]:
        _emit(ctx, {"dry_run": True, "steps": steps})
        return
    _confirm(ctx, yes, f"rotate key {alias} (old secret stops working)")
    _call(be.post, c, "/key/update", steps[0]["body"])
    try:
        new = _call(be.post, c, "/key/generate", new_body)
    except click.ClickException:
        _call(be.post, c, "/key/update", {"key": token, "key_alias": alias})  # put it back
        raise
    _call(be.post, c, "/key/delete", steps[2]["body"])
    _emit(
        ctx,
        {
            "rotated": alias,
            "key": new.get("key"),
            "expires": new.get("expires"),
            "carried_over": sorted(k for k in new_body if k != "duration"),
        },
    )


# ── teams ───────────────────────────────────────────────────────────────────


@cli.group("teams")
def teams_grp():
    """Teams: members, budgets, model access."""


@teams_grp.command("list")
@click.pass_context
def teams_list(ctx):
    res = _call(be.get, _conn(ctx), "/team/list") or []
    rows = [
        {
            "team_id": t.get("team_id"),
            "alias": t.get("team_alias"),
            "members": len(t.get("members_with_roles") or []),
            "spend": round(t.get("spend") or 0, 4),
            "max_budget": t.get("max_budget"),
            "models": t.get("models"),
            "blocked": t.get("blocked"),
        }
        for t in res
    ]
    _emit(
        ctx,
        rows,
        lambda r: _table(
            r,
            [
                ("alias", "TEAM"),
                ("team_id", "ID"),
                ("members", "MEMBERS"),
                ("spend", "SPEND"),
                ("max_budget", "BUDGET"),
                ("blocked", "BLOCKED"),
            ],
        ),
    )


@teams_grp.command("info")
@click.argument("team_id")
@click.pass_context
def teams_info(ctx, team_id):
    _emit(ctx, _call(be.get, _conn(ctx), "/team/info", params={"team_id": team_id}))


# ── spend ───────────────────────────────────────────────────────────────────


@cli.command("spend")
@click.option("--days", default=7, type=int)
@click.option("--by", "group_by", type=click.Choice(["model", "key", "team", "user"]), default="model")
@click.pass_context
def spend(ctx, days, group_by):
    """Spend and request totals over the last N days, grouped client-side from /spend/logs."""
    c = _conn(ctx)
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=days + 1)
    logs = (
        _call(
            be.get,
            c,
            "/spend/logs",
            params={"start_date": start.isoformat(), "end_date": end.isoformat(), "summarize": "false"},
        )
        or []
    )
    field = {"model": "model_group", "key": "api_key", "team": "team_id", "user": "user"}[group_by]
    agg: dict[str, dict[str, Any]] = {}
    alias: dict[str, str] = {}
    if group_by == "key":
        alias = {k.get("token"): k.get("key_alias") or k.get("key_name") for k in _call(all_keys, c)}
        if c.get("key"):  # LiteLLM stores sha256(key); name the one we authenticate with
            alias[hashlib.sha256(c["key"].encode()).hexdigest()] = "(master key)"
    for row in logs if isinstance(logs, list) else []:
        g = row.get(field) or row.get("model") or "(rejected — no model)"
        if group_by == "key" and g not in alias:
            # The log row records the alias at request time, so a key deleted
            # or rotated since is still named correctly.
            logged = (row.get("metadata") or {}).get("user_api_key_alias")
            alias[g] = f"{logged} (gone)" if logged else f"(unknown {g[:10]}…)"
        a = agg.setdefault(
            g,
            {
                group_by: alias.get(g, g),
                "requests": 0,
                "spend": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
        )
        a["requests"] += 1
        a["spend"] += row.get("spend") or 0
        a["prompt_tokens"] += row.get("prompt_tokens") or 0
        a["completion_tokens"] += row.get("completion_tokens") or 0
    rows = sorted(agg.values(), key=lambda r: r["spend"], reverse=True)
    for r in rows:
        r["spend"] = round(r["spend"], 4)
    data = {
        "days": days,
        "group_by": group_by,
        "log_rows": len(logs) if isinstance(logs, list) else 0,
        "rows": rows,
    }
    _emit(
        ctx,
        data,
        lambda d: _table(
            d["rows"],
            [
                (group_by, group_by.upper()),
                ("requests", "REQUESTS"),
                ("spend", "SPEND"),
                ("prompt_tokens", "PROMPT"),
                ("completion_tokens", "COMPLETION"),
            ],
        ),
    )


# ── drift / lint / fleet ────────────────────────────────────────────────────


@cli.command("drift")
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True),
    help="The config.yaml you deploy (the one in git).",
)
@click.pass_context
def drift(ctx, config_path):
    """Git config vs the live proxy: deployments, DB-only models, router, guardrails."""
    c = _conn(ctx)
    res = drift_mod.config_vs_live(
        _load_yaml(config_path), _call(live_deployments, c), _call(live_router, c), _call(live_guardrails, c)
    )
    _emit(ctx, res)
    if not res["in_sync"]:
        ctx.exit(2)


@cli.command("lint")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True))
@click.option(
    "--policy",
    "policy_path",
    default=None,
    type=click.Path(exists=True),
    help="Site rules (YAML) — see `lint --help-policy`.",
)
@click.option("--help-policy", is_flag=True, help="Print the policy file format.")
@click.pass_context
def lint(ctx, config_path, policy_path, help_policy):
    """Static checks on a config.yaml (no proxy needed). Exit 1 on errors."""
    if help_policy:
        click.echo(lint_mod.__doc__)
        return
    res = lint_mod.lint(_load_yaml(config_path), _load_yaml(policy_path) if policy_path else None)
    _emit(
        ctx,
        res,
        lambda r: (
            click.echo(f"  {r['level'].upper()}: {r['count']} finding(s)"),
            _table(
                r["findings"],
                [("level", "LEVEL"), ("rule", "RULE"), ("where", "WHERE"), ("message", "MESSAGE")],
            ),
        ),
    )
    if res["level"] == "error":
        ctx.exit(1)


@cli.group("fleet")
def fleet_grp():
    """Several proxies behind one VIP must be identical."""


@fleet_grp.command("diff")
@click.option(
    "--node",
    "nodes",
    multiple=True,
    required=True,
    help="Proxy URL; repeat for each node (first is the reference).",
)
@click.pass_context
def fleet_diff(ctx, nodes):
    """Compare nodes: version, deployments, router, guardrails, key visibility."""
    if len(nodes) < 2:
        raise click.ClickException("need at least two --node URLs")
    snaps: dict[str, dict[str, Any]] = {}
    for n in nodes:
        c = _conn(ctx, n)
        try:
            snaps[n] = {
                "version": live_version(c),
                "deployments": live_deployments(c),
                "router": live_router(c),
                "guardrails": live_guardrails(c),
                "keys": (be.get(c, "/key/list", params={"size": 1}) or {}).get("total_count"),
            }
        except be.ApiError as e:
            hint = (
                " — this node rejects the key: master keys differ between nodes?" if e.status == 401 else ""
            )
            snaps[n] = {"error": f"{e}{hint}"}
    res = drift_mod.fleet_diff(snaps)
    _emit(ctx, res)
    if not res["identical"]:
        ctx.exit(2)


def main() -> None:
    cli(prog_name="cli-anything-litellm")


if __name__ == "__main__":
    main()
