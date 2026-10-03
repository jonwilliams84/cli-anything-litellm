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
from cli_anything.litellm.core import budgets as budgets_mod
from cli_anything.litellm.core import drift as drift_mod
from cli_anything.litellm.core import lint as lint_mod
from cli_anything.litellm.core import models as models_mod
from cli_anything.litellm.core import teams as teams_mod
from cli_anything.litellm.core import users as users_mod


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


@models_grp.command("add")
@click.option("--name", required=True, help="Model group clients request.")
@click.option("--model", required=True, help="LiteLLM provider string, e.g. hosted_vllm/qwen.")
@click.option("--api-base", default=None, help="Backend base URL, e.g. http://n1:8000/v1.")
@click.option("--api-key", default=None, help="Backend key; use os.environ/NAME so the proxy resolves it.")
@click.option(
    "--mode",
    default=None,
    type=click.Choice(
        ["chat", "embedding", "completion", "image_generation", "audio_transcription", "rerank"]
    ),
)
@click.option("--max-input-tokens", "max_input_tokens", type=int, default=None)
@click.pass_context
def models_add(ctx, name, model, api_base, api_key, mode, max_input_tokens):
    """Add a deployment to the proxy's database (POST /model/new).

    What the Admin UI does, from the shell — a DB-only model: it survives a
    config redeploy and `drift` will report it as `only_live / db_model`, so it
    belongs in git too if it must live forever.
    """
    try:
        body = models_mod.new_deployment(
            name, model, api_base=api_base, api_key=api_key, mode=mode, max_input_tokens=max_input_tokens
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if _dry(ctx, "POST", "/model/new", body):
        return
    res = _call(be.post, _conn(ctx), "/model/new", body) or {}
    _emit(
        ctx,
        res,
        lambda r: click.echo(
            f"  added {name} ({((r.get('data') or {}).get('model_info') or {}).get('id', 'id unknown')})"
        ),
    )


@models_grp.command("delete")
@click.argument("model_or_id")
@click.option("--yes", is_flag=True)
@click.pass_context
def models_delete(ctx, model_or_id, yes):
    """Remove a deployment from the proxy's database (POST /model/delete).

    MODEL_OR_ID is a deployment id (deletes one replica) or a model group name
    (deletes every replica of it). Use `drift --config` first: a deployment the
    config still describes comes back on the next redeploy; a DB-only one does
    not come back.
    """
    c = _conn(ctx)
    deps = _call(live_deployments, c)
    target = models_mod.resolve_model_target(deps, model_or_id)
    if not target:
        raise click.ClickException(
            f"no live deployment matches {model_or_id!r} (see `models list --deployments`)"
        )
    body = {target["by"]: model_or_id}
    kind = (
        f"model group {model_or_id} (all {sum(1 for d in deps if d.get('model_name') == model_or_id)} replicas)"
        if target["by"] == "model_name"
        else f"deployment {model_or_id}"
    )
    if _dry(ctx, "POST", "/model/delete", body):
        return
    _confirm(ctx, yes, f"delete {kind}")
    res = _call(be.post, c, "/model/delete", body) or {}
    _emit(ctx, res, lambda r: click.echo(f"  deleted {kind}: {r.get('message') or 'ok'}"))


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
@click.option("--budget-id", "budget_id", default=None, help="Attach a shared budget (`budgets create`).")
@click.option("--rpm", type=int, default=None)
@click.option("--tpm", type=int, default=None)
@click.pass_context
def keys_generate(ctx, alias, team, user, models, duration, max_budget, budget_id, rpm, tpm):
    body = {
        k: v
        for k, v in {
            "key_alias": alias,
            "team_id": team,
            "user_id": user,
            "models": models.split(",") if models else None,
            "duration": duration,
            "max_budget": max_budget,
            "budget_id": budget_id,
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
@click.option("--budget-id", "budget_id", default=None, help="Attach a shared budget (`budgets create`).")
@click.option("--rpm", type=int, default=None)
@click.option("--tpm", type=int, default=None)
@click.option("--duration", default=None)
@click.pass_context
def keys_update(ctx, key_or_alias, models, max_budget, budget_id, rpm, tpm, duration):
    c = _conn(ctx)
    body = {
        k: v
        for k, v in {
            "models": models.split(",") if models else None,
            "max_budget": max_budget,
            "budget_id": budget_id,
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
    "budget_id",
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


TEAM_COLS = [
    ("alias", "TEAM"),
    ("team_id", "ID"),
    ("members", "MEMBERS"),
    ("spend", "SPEND"),
    ("max_budget", "BUDGET"),
    ("blocked", "BLOCKED"),
]


@cli.group("teams")
def teams_grp():
    """Teams: members, budgets, model access."""


def all_teams(conn) -> list[dict]:
    """The live teams, as DELETE/UPDATE/BLOCK need them for id resolution."""
    return be.get(conn, "/team/list") or []


@teams_grp.command("list")
@click.pass_context
def teams_list(ctx):
    rows = [teams_mod.normalize(t) for t in _call(all_teams, _conn(ctx))]
    _emit(ctx, rows, lambda r: _table(r, TEAM_COLS))


@teams_grp.command("info")
@click.argument("team_id")
@click.pass_context
def teams_info(ctx, team_id):
    _emit(ctx, _call(be.get, _conn(ctx), "/team/info", params={"team_id": team_id}))


def _resolve_team(conn, ref: str) -> dict[str, Any]:
    """The live team record for `ref` (team_id or unique alias), or a Click error."""
    t = teams_mod.resolve_team_target(_call(all_teams, conn), ref)
    if not t:
        raise click.ClickException(f"no live team matches {ref!r} — run `teams list`")
    return t["team"]


@teams_grp.command("create")
@click.option("--alias", required=True, help="Team label shown in tables and in drift reports.")
@click.option(
    "--team-id", default=None, help="Id keys and users reference; the proxy generates one if omitted."
)
@click.option("--models", default=None, help="Comma-separated model groups the team may use; default all.")
@click.option("--max-budget", type=float, default=None)
@click.option("--rpm", type=int, default=None)
@click.option("--tpm", type=int, default=None)
@click.option("--budget-duration", default=None, help="Budget reset cycle, e.g. 30d.")
@click.option("--member", "members", multiple=True, help="User name or email to add; repeat for each.")
@click.option(
    "--member-role", default=None, type=click.Choice(["admin", "user"]), help="For the members above."
)
@click.pass_context
def teams_create(ctx, alias, team_id, models, max_budget, rpm, tpm, budget_duration, members, member_role):
    """POST /team/new. Members named by email get a proxy account created for them."""
    try:
        body = teams_mod.new_team(
            alias,
            team_id,
            models=models,
            max_budget=max_budget,
            rpm=rpm,
            tpm=tpm,
            budget_duration=budget_duration,
            members=members,
            member_role=member_role,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if _dry(ctx, "POST", "/team/new", body):
        return
    res = _call(be.post, _conn(ctx), "/team/new", body)
    if isinstance(res, dict):
        res = {
            "created": res.get("team_id") or team_id or alias,
            "team_alias": res.get("team_alias") or alias,
            **res,
        }
    _emit(ctx, res)


@teams_grp.command("update")
@click.argument("team_ref")
@click.option("--alias", default=None)
@click.option("--models", default=None, help="Comma-separated model groups (replaces the set).")
@click.option("--max-budget", type=float, default=None)
@click.option("--rpm", type=int, default=None)
@click.option("--tpm", type=int, default=None)
@click.option("--budget-duration", default=None)
@click.pass_context
def teams_update(ctx, team_ref, alias, models, max_budget, rpm, tpm, budget_duration):
    """POST /team/update. Given flags replace the field; omitted ones stay as-is."""
    t = _resolve_team(_conn(ctx), team_ref)
    try:
        body = teams_mod.update_team(
            t["team_id"],
            alias=alias,
            models=models,
            max_budget=max_budget,
            rpm=rpm,
            tpm=tpm,
            budget_duration=budget_duration,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if _dry(ctx, "POST", "/team/update", body):
        return
    _emit(ctx, _call(be.post, _conn(ctx), "/team/update", body))


@teams_grp.command("delete")
@click.argument("team_ref")
@click.option("--yes", is_flag=True)
@click.pass_context
def teams_delete(ctx, team_ref, yes):
    """POST /team/delete. Also deletes every virtual key minted for the team."""
    c = _conn(ctx)
    t = _resolve_team(c, team_ref)
    team_id = t["team_id"]
    body = {"team_ids": [team_id]}
    if _dry(ctx, "POST", "/team/delete", body):
        return
    keys = [_key_row(k) for k in _call(all_keys, c, team_id)]
    _confirm(ctx, yes, f"delete team {team_ref} and the {len(keys)} key(s) minted for it")
    _call(be.post, c, "/team/delete", body)
    _emit(
        ctx,
        {
            "deleted": body,
            "keys_removed": [{"alias": k.get("key_alias"), "token": k.get("token")} for k in keys],
        },
        lambda d: (
            click.echo(f"deleted team {team_id}"),
            click.echo(
                "keys no longer valid: "
                + (", ".join(k["alias"] or k["token"] for k in d["keys_removed"]) or "(none)")
            ),
        ),
    )


@teams_grp.command("member-add")
@click.argument("team_ref")
@click.option("--user", required=True, help="User name or email; an email without an account is created.")
@click.option(
    "--role",
    "role",
    default=None,
    type=click.Choice(["admin", "user"]),
    help=f"Default: {teams_mod.DEFAULT_MEMBER_ROLE}.",
)
@click.pass_context
def teams_member_add(ctx, team_ref, user, role):
    """POST /team/member_add. The member starts inheriting the team's budget and models."""
    c = _conn(ctx)
    team_id = _resolve_team(c, team_ref)["team_id"]
    if "@" not in user and not users_mod.resolve_user_target(_call(all_users, c), user):
        raise click.ClickException(f"no user matches {user!r} — run `users list` (or pass their email)")
    body = {"team_id": team_id, "member": [teams_mod.member_entry(user, role)]}
    if _dry(ctx, "POST", "/team/member_add", body):
        return
    _emit(ctx, _call(be.post, c, "/team/member_add", body))


@teams_grp.command("member-delete")
@click.argument("team_ref")
@click.option("--user", required=True, help="User name or email on the team.")
@click.pass_context
def teams_member_delete(ctx, team_ref, user):
    """POST /team/member_delete. The user's own keys keep working; team keys stay with the team."""
    c = _conn(ctx)
    team_id = _resolve_team(c, team_ref)["team_id"]
    body = (
        {"team_id": team_id, "user_id": user} if "@" not in user else {"team_id": team_id, "user_email": user}
    )
    if _dry(ctx, "POST", "/team/member_delete", body):
        return
    _emit(ctx, _call(be.post, c, "/team/member_delete", body))


def _simple_team_action(name: str, path: str, destructive: bool):
    @teams_grp.command(name)
    @click.argument("team_ref")
    @click.option("--yes", is_flag=True)
    @click.pass_context
    def _cmd(ctx, team_ref, yes):
        c = _conn(ctx)
        team_id = _resolve_team(c, team_ref)["team_id"]
        body = {"team_ids": [team_id]}
        if _dry(ctx, "POST", path, body):
            return
        if destructive:
            _confirm(ctx, yes, f"{name} team {team_ref}")
        _emit(ctx, _call(be.post, c, path, body))

    _cmd.__doc__ = {
        "block": "Block a team (all of its keys error with 'Key blocked ...'); reversible with unblock.",
        "unblock": "Unblock a team.",
    }[name]
    return _cmd


_simple_team_action("block", "/team/block", True)
_simple_team_action("unblock", "/team/unblock", False)


# ── budgets ─────────────────────────────────────────────────────────────────


BUDGET_COLS = [
    ("budget_id", "BUDGET"),
    ("max_budget", "MAX"),
    ("soft_budget", "SOFT"),
    ("budget_duration", "RESET"),
    ("tpm_limit", "TPM"),
    ("rpm_limit", "RPM"),
    ("max_parallel_requests", "PARALLEL"),
]


@cli.group("budgets")
def budgets_grp():
    """Reusable spend limits that keys, teams and users reference by id.

    A budget carries the cap, reset cycle and per-model maxima that every
    key attached with `--budget-id` inherits — change it once instead of
    re-pricing each key.
    """


def all_budgets(conn) -> list[dict]:
    """The live budgets (the /budget/list rows, whatever envelope they come in)."""
    return budgets_mod.as_rows(be.get(conn, "/budget/list") or {})


def _resolve_budget(conn, budget_id: str) -> dict[str, Any]:
    """Refuse an unknown budget_id before mutating; budgets have no alias."""
    t = budgets_mod.resolve_budget_target(_call(all_budgets, conn), budget_id)
    if not t:
        raise click.ClickException(f"no live budget matches {budget_id!r} — run `budgets list`")
    return t["budget"]


@budgets_grp.command("list")
@click.pass_context
def budgets_list(ctx):
    rows = [budgets_mod.normalize(b) for b in _call(all_budgets, _conn(ctx))]
    _emit(ctx, rows, lambda r: _table(r, BUDGET_COLS))


@budgets_grp.command("info")
@click.argument("budget_id")
@click.pass_context
def budgets_info(ctx, budget_id):
    """The budget and every key / team that references it."""
    res = _call(be.get, _conn(ctx), "/budget/info", params={"budget_id": budget_id})
    res = res if isinstance(res, dict) else {}
    row = res.get("info")
    if row is None:  # some proxies put the row directly in the response
        row = {k: v for k, v in res.items() if k not in ("keys", "teams")}
    data = {
        "budget": budgets_mod.normalize(row),
        "keys": res.get("keys") or [],
        "teams": res.get("teams") or [],
    }

    def human(d):
        click.echo(f"  keys {len(d['keys'])}  teams {len(d['teams'])} on this budget")
        for k in d["keys"]:
            click.echo(f"    key {(k.get('key_alias') or k.get('key_name')) or '?'}")

    _emit(ctx, data, human)


@budgets_grp.command("create")
@click.option(
    "--budget-id",
    "budget_id",
    default=None,
    help="The id keys reference; the proxy generates one if omitted.",
)
@click.option("--max-budget", type=float, default=None, help="Hard spend cap per reset period.")
@click.option(
    "--soft-budget", type=float, default=None, help="Warn (or trigger the soft-budget hook) past this."
)
@click.option("--rpm", type=int, default=None)
@click.option("--tpm", type=int, default=None)
@click.option("--parallel", type=int, default=None, help="Max parallel requests per key.")
@click.option(
    "--duration", default=None, help="Reset cycle, e.g. 30d — LiteLLM resets max_budget every cycle."
)
@click.option(
    "--model-max-budget",
    default=None,
    help="JSON object of per-model caps, e.g. '{\"gpt-4o\": 0.01}'.",
)
@click.pass_context
def budgets_create(ctx, budget_id, max_budget, soft_budget, rpm, tpm, parallel, duration, model_max_budget):
    """POST /budget/new. Attach keys afterwards: `keys generate --budget-id`."""
    try:
        body = budgets_mod.new_budget(
            budget_id,
            max_budget=max_budget,
            soft_budget=soft_budget,
            rpm=rpm,
            tpm=tpm,
            parallel=parallel,
            duration=duration,
            model_max_budget=model_max_budget,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if _dry(ctx, "POST", "/budget/new", body):
        return
    res = _call(be.post, _conn(ctx), "/budget/new", body)
    _emit(ctx, res, lambda r: click.echo(f"  created {(r.get('budget_id') or budget_id) or 'a budget'}"))


@budgets_grp.command("update")
@click.argument("budget_id")
@click.option("--max-budget", type=float, default=None)
@click.option("--soft-budget", type=float, default=None)
@click.option("--rpm", type=int, default=None)
@click.option("--tpm", type=int, default=None)
@click.option("--parallel", type=int, default=None)
@click.option("--duration", default=None)
@click.option(
    "--model-max-budget",
    default=None,
    help="JSON object of per-model caps (replaces the set).",
)
@click.pass_context
def budgets_update(ctx, budget_id, max_budget, soft_budget, rpm, tpm, parallel, duration, model_max_budget):
    """POST /budget/update. Applies at once to every key, team and user on it."""
    c = _conn(ctx)
    _resolve_budget(c, budget_id)
    try:
        body = budgets_mod.update_budget(
            budget_id,
            max_budget=max_budget,
            soft_budget=soft_budget,
            rpm=rpm,
            tpm=tpm,
            parallel=parallel,
            duration=duration,
            model_max_budget=model_max_budget,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if _dry(ctx, "POST", "/budget/update", body):
        return
    _emit(ctx, _call(be.post, c, "/budget/update", body))


@budgets_grp.command("delete")
@click.argument("budget_id")
@click.option("--yes", is_flag=True)
@click.pass_context
def budgets_delete(ctx, budget_id, yes):
    """POST /budget/delete. Keys, teams and users on it keep working with their own limits."""
    c = _conn(ctx)
    _resolve_budget(c, budget_id)
    # The budget family says budget_id everywhere except delete, which says id.
    body = {budgets_mod.DELETE_KEY: budget_id}
    if _dry(ctx, "POST", "/budget/delete", body):
        return
    _confirm(ctx, yes, f"delete budget {budget_id}")
    _call(be.post, c, "/budget/delete", body)
    _emit(ctx, {"deleted": budget_id}, None)


# ── users ───────────────────────────────────────────────────────────────────

USER_COLS = [
    ("user_id", "USER_ID"),
    ("email", "EMAIL"),
    ("role", "ROLE"),
    ("teams", "TEAMS"),
    ("spend", "SPEND"),
    ("max_budget", "BUDGET"),
    ("blocked", "BLOCKED"),
]

USERS_PAGE = 100


def all_users(conn, team: str | None = None, role: str | None = None) -> list[dict]:
    """Every user, following /user/get_users pagination (same loop as keys)."""
    out, page = [], 1
    while True:
        res = be.get(conn, "/user/get_users", params={"page": page, "size": USERS_PAGE}) or {}
        rows = res.get("users") or []
        if team or role:
            rows = [
                u
                for u in rows
                if (not team or team in (u.get("teams") or [])) and (not role or u.get("user_role") == role)
            ]
        out += rows
        if page >= (res.get("total_pages") or 1):
            return out
        page += 1


@cli.group("users")
def users_grp():
    """Proxy accounts: roles, team membership, budgets — keys hang off these."""


@users_grp.command("list")
@click.option("--team", default=None, help="Only users on this team (id or alias).")
@click.option("--role", default=None, help="Only this user_role (e.g. proxy_admin).")
@click.pass_context
def users_list(ctx, team, role):
    rows = [users_mod.normalize(u) for u in _call(all_users, _conn(ctx), team, role)]
    _emit(ctx, rows, lambda r: _table(r, USER_COLS))


@users_grp.command("info")
@click.argument("user_ref")
@click.pass_context
def users_info(ctx, user_ref):
    """By user name or email; also the virtual keys minted for them."""
    c = _conn(ctx)
    # /user/info is keyed by user_id; an email is resolved to a user first.
    user_id = user_ref
    if "@" in user_ref:
        t = users_mod.resolve_user_target(_call(all_users, c), user_ref)
        if not t:
            raise click.ClickException(f"no user matches {user_ref!r} — run `users list`")
        user_id = t["user"]["user_id"]
    res = _call(be.get, c, "/user/info", params={"user_id": user_id}) or {}
    _emit(
        ctx,
        {
            "user": users_mod.normalize(res.get("user_info") or res),
            "keys": [
                {"alias": k.get("key_alias"), "token": k.get("token"), "expires": k.get("expires")}
                for k in (res.get("keys") or [])
            ],
        },
    )


@users_grp.command("create")
@click.option("--user-id", default=None, help="User name apps authenticate / own keys as.")
@click.option("--email", default=None)
@click.option(
    "--role",
    default=None,
    help=f"LiteLLM role (default: {users_mod.DEFAULT_ROLE}).",
)
@click.option("--teams", default=None, help="Comma-separated team ids.")
@click.option("--max-budget", type=float, default=None)
@click.option("--rpm", type=int, default=None)
@click.option("--tpm", type=int, default=None)
@click.pass_context
def users_create(ctx, user_id, email, role, teams, max_budget, rpm, tpm):
    """POST /user/new. Never mints a key — issue one with `keys generate --user`."""
    try:
        body = users_mod.new_user(user_id, email, role, teams, max_budget, rpm, tpm)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if _dry(ctx, "POST", "/user/new", body):
        return
    res = _call(be.post, _conn(ctx), "/user/new", body)
    if isinstance(res, dict):
        res = {"created": res.get("user_id") or user_id or email, **res}
    _emit(ctx, res)


@users_grp.command("update")
@click.argument("user_ref")
@click.option("--role", default=None)
@click.option("--teams", default=None, help="Comma-separated team ids (replaces the set).")
@click.option("--max-budget", type=float, default=None)
@click.option("--rpm", type=int, default=None)
@click.option("--tpm", type=int, default=None)
@click.pass_context
def users_update(ctx, user_ref, role, teams, max_budget, rpm, tpm):
    """POST /user/update. Given flags replace the field; omitted ones stay as-is."""
    try:
        body = users_mod.update_user(
            user_id=user_ref if "@" not in user_ref else None,
            email=user_ref if "@" in user_ref else None,
            role=role,
            teams=teams,
            max_budget=max_budget,
            rpm=rpm,
            tpm=tpm,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if _dry(ctx, "POST", "/user/update", body):
        return
    _emit(ctx, _call(be.post, _conn(ctx), "/user/update", body))


@users_grp.command("delete")
@click.argument("user_ref")
@click.option("--yes", is_flag=True)
@click.pass_context
def users_delete(ctx, user_ref, yes):
    """POST /user/delete. Also invalidates every virtual key the user owns."""
    c = _conn(ctx)
    t = users_mod.resolve_user_target(_call(all_users, c), user_ref)
    if not t:
        extra = f" (several users share email {user_ref!r}: delete by user_id)" if "@" in user_ref else ""
        raise click.ClickException(f"no live user matches {user_ref!r} — run `users list`{extra}")
    body = {"user_id": t["user"].get("user_id")}
    if _dry(ctx, "POST", "/user/delete", body):
        return
    keys = [_key_row(k) for k in _call(all_keys, c) if k.get("user_id") == t["user"].get("user_id")]
    _confirm(ctx, yes, f"delete user {user_ref} and the {len(keys)} key(s) they own")
    _call(be.post, c, "/user/delete", body)
    _emit(
        ctx,
        {
            "deleted": body,
            "keys_removed": [{"alias": k.get("key_alias"), "token": k.get("token")} for k in keys],
        },
        lambda d: (
            click.echo(f"deleted user {body['user_id']}"),
            click.echo(
                "keys no longer valid: "
                + (", ".join(k["alias"] or k["token"] for k in d["keys_removed"]) or "(none)")
            ),
        ),
    )


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
