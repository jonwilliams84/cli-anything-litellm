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
from urllib.parse import quote
from typing import Any

import click
import yaml

from cli_anything.litellm import __version__
from cli_anything.litellm.core import backend as be
from cli_anything.litellm.core import budgets as budgets_mod
from cli_anything.litellm.core import customers as customers_mod
from cli_anything.litellm.core import drift as drift_mod
from cli_anything.litellm.core import fallbacks as fb_mod
from cli_anything.litellm.core import guardrails as guard_mod
from cli_anything.litellm.core import health as health_mod
from cli_anything.litellm.core import lint as lint_mod
from cli_anything.litellm.core import logs as logs_mod
from cli_anything.litellm.core import mcp as mcp_mod
from cli_anything.litellm.core import models as models_mod
from cli_anything.litellm.core import tags as tags_mod
from cli_anything.litellm.core import teams as teams_mod
from cli_anything.litellm.core import users as users_mod
from cli_anything.litellm.core import usage as usage_mod


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


def _api_error(e: be.ApiError) -> click.ClickException:
    hint = ""
    if e.status == 401:
        hint = (
            "  (wrong key — behind a VIP, a 401 on SOME requests usually means the "
            "gateways' master keys differ)"
        )
    return click.ClickException(f"{e}{hint}")


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except be.ApiError as e:
        raise _api_error(e) from e


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


@cli.group("health", invoke_without_command=True)
@click.option("--model", default=None, help="Only this model group.")
@click.pass_context
def health(ctx, model):
    """Per-deployment health (GET /health). Sends a real request to every backend.

    `health latest` and `health history` read what the proxy's own health
    checks recorded instead, and call no backend.
    """
    if ctx.invoked_subcommand is not None:
        if model:
            raise click.UsageError("--model belongs to `health` alone or to `health history --model`")
        return
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


def _short(text: str | None, width: int = 100) -> str | None:
    """One table line: whitespace (newlines from upstream JSON bodies) collapsed, then cut."""
    if not text:
        return text
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _health_note(r: dict) -> str:
    return "gone" if r.get("live") is False else ("blocked" if r.get("blocked") else "")


@health.command("latest")
@click.option("--unhealthy", "only_unhealthy", is_flag=True, help="Only rows that are not healthy.")
@click.pass_context
def health_latest(ctx, only_unhealthy):
    """Last recorded status of every deployment (GET /health/latest). Calls no backend.

    Joined to the live deployments: SINCE is when that status was recorded
    (background checks record only status changes), `gone` marks a row whose
    deployment no longer exists, `blocked` one an admin paused, and a live
    deployment never checked shows as `unknown`. Exit 2 when a live, unpaused
    deployment is unhealthy.
    """
    c = _conn(ctx)
    res = _call(be.get, c, "/health/latest")
    rows = health_mod.latest_rows(res, _call(live_deployments, c))
    if only_unhealthy:
        rows = [r for r in rows if r["status"] != "healthy"]
    _emit(
        ctx,
        rows,
        lambda r: _table(
            [dict(x, note=_health_note(x), error=_short(x["error"])) for x in r],
            [
                ("status", "STATUS"),
                ("model_name", "MODEL"),
                ("model_id", "ID"),
                ("checked_at", "SINCE"),
                ("response_time_ms", "MS"),
                ("note", "NOTE"),
                ("error", "ERROR"),
            ],
        ),
    )
    if any(r["status"] == "unhealthy" and r["live"] and not r["blocked"] for r in rows):
        ctx.exit(2)


@health.command("history")
@click.option("--model", default=None, help="Only this model group (model_name).")
@click.option("--status", type=click.Choice(health_mod.STATUSES), default=None)
@click.option("--limit", type=int, default=50, show_default=True, help=f"1..{health_mod.HISTORY_MAX}.")
@click.option("--offset", type=int, default=0, show_default=True, help="Skip this many newer records.")
@click.pass_context
def health_history(ctx, model, status, limit, offset):
    """Recorded health checks, newest first (GET /health/history). Calls no backend.

    Background checks record a row only when a deployment's status changes, so
    this reads as "when did it go down, when did it come back".
    """
    try:
        params = health_mod.history_params(model, status, limit, offset)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    res = _call(be.get, _conn(ctx), "/health/history", params=params)
    rows = [health_mod.normalize(r) for r in health_mod.as_rows(res, "health_checks")]

    def human(r):
        _table(
            [dict(x, error=_short(x["error"])) for x in r],
            [
                ("checked_at", "WHEN"),
                ("status", "STATUS"),
                ("model_name", "MODEL"),
                ("model_id", "ID"),
                ("checked_by", "BY"),
                ("error", "ERROR"),
            ],
        )
        if len(r) == limit:
            click.echo(f"  (more: --offset {offset + limit})")

    _emit(ctx, rows, human)


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
                "blocked": models_mod.is_blocked(d),
            }
            for d in deps
        ]
        _emit(
            ctx,
            rows,
            lambda r: _table(
                [dict(x, paused="yes" if x["blocked"] else "") for x in r],
                [
                    ("model_name", "MODEL"),
                    ("id", "ID"),
                    ("api_base", "BACKEND"),
                    ("max_input_tokens", "CTX"),
                    ("source", "SRC"),
                    ("paused", "PAUSED"),
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
                ("blocked_replicas", "PAUSED"),
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


def _set_blocked(ctx, model_id: str, blocked: bool, yes: bool = False) -> None:
    verb = "block" if blocked else "unblock"
    c = _conn(ctx)
    deps = _call(live_deployments, c)
    try:
        d = models_mod.resolve_deployment(deps, model_id, verb)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    group = d.get("model_name")
    serving = models_mod.serving_replicas(deps, group, excluding=model_id)
    out = {"model_id": model_id, "model_name": group, "blocked": blocked, "serving_replicas": serving}
    if models_mod.is_blocked(d) == blocked:
        _emit(ctx, {**out, "changed": False}, f"  {model_id} is already {verb}ed")
        return
    body = {"model_id": model_id}
    if _dry(ctx, "POST", f"/model/{verb}", body):
        return
    if blocked:
        impact = (
            f"{serving} other replica(s) of {group} keep serving"
            if serving
            else f"the LAST serving replica: {group} stops serving unless a fallback catches it"
        )
        _confirm(ctx, yes, f"block deployment {model_id} ({impact})")
    res = _call(be.post, c, f"/model/{verb}", body)
    # The reply is the DB row, litellm_params included: never echo it.
    got = res.get("blocked") if isinstance(res, dict) else None
    out = {**out, "blocked": blocked if got is None else bool(got), "changed": True}
    if not blocked:
        out["serving_replicas"] = serving + 1
    _emit(
        ctx,
        out,
        lambda r: click.echo(f"  {verb}ed {model_id} ({group}: {r['serving_replicas']} replica(s) serving)"),
    )


@models_grp.command("block")
@click.argument("model_id")
@click.option("--yes", is_flag=True)
@click.pass_context
def models_block(ctx, model_id, yes):
    """Pause one DB deployment: the router stops sending it traffic (POST /model/block).

    Reversible with `models unblock`; the deployment and its settings stay. The
    proxy pauses DB deployments only (config ones: change the config). Takes a
    deployment id, never a group name — blocking every replica is an outage.
    """
    _set_blocked(ctx, model_id, True, yes)


@models_grp.command("unblock")
@click.argument("model_id")
@click.pass_context
def models_unblock(ctx, model_id):
    """Put a paused DB deployment back into rotation (POST /model/unblock)."""
    _set_blocked(ctx, model_id, False)


@cli.group("router")
def router_grp():
    """Live router settings."""


@router_grp.command("show")
@click.option("--all", "show_all", is_flag=True, help="Every field, not just the ones that matter.")
@click.pass_context
def router_show(ctx, show_all):
    v = _call(live_router, _conn(ctx))
    if not show_all:
        keep = (*drift_mod.ROUTER_KEYS, *fb_mod.FIELDS.values())
        v = {k: v.get(k) for k in keep if k in v}
    _emit(ctx, v)


@cli.group("fallbacks")
def fallbacks_grp():
    """Router fallbacks: which model group takes over when another fails.

    Three lists, one per failure class (--type): general (any error),
    context_window, content_policy. A list config.yaml sets is owned by the
    config: `set` and `delete` refuse it (the proxy would change one worker
    until its next restart).
    """


def _router_settings(conn) -> dict[str, Any]:
    return be.get(conn, "/router/settings") or {}


def _fallback_type_option(fn):
    return click.option(
        "--type",
        "fallback_type",
        type=click.Choice(fb_mod.TYPES),
        default="general",
        show_default=True,
        help="Failure class the rule covers.",
    )(fn)


def _chain_text(chain) -> str:
    return " → ".join(chain or []) or "(none)"


def _refuse_config_owned(settings: dict[str, Any], fallback_type: str) -> None:
    if fb_mod.config_owned(settings.get("source"), fallback_type):
        field = fb_mod.FIELDS[fallback_type]
        raise click.ClickException(
            f"router_settings.{field} is set in config.yaml, which owns it: the proxy would change "
            "only the worker that answered, keep showing the config value, and drop the change on "
            "restart. Edit the config and redeploy (then `drift`)."
        )


@fallbacks_grp.command("list")
@click.option(
    "--model", default=None, help="Only rules that apply to this group (its own, plus * catch-alls)."
)
@click.option("--type", "fallback_type", type=click.Choice(fb_mod.TYPES), default=None)
@click.pass_context
def fallbacks_list(ctx, model, fallback_type):
    """Every fallback rule the live router holds (GET /router/settings), with its owner.

    SRC is config (config.yaml) or db (set through the API or the Admin UI —
    not in git; `drift` reports it).
    """
    res = _call(_router_settings, _conn(ctx))
    rows = fb_mod.list_rows(
        res.get("current_values"), res.get("source"), model=model, fallback_type=fallback_type
    )
    _emit(
        ctx,
        rows,
        lambda r: _table(
            [dict(x, chain=_chain_text(x["fallbacks"])) for x in r],
            [("model", "MODEL"), ("type", "TYPE"), ("chain", "FALLBACKS"), ("source", "SRC")],
        ),
    )


def _resolve_chain(conn, model: str, fallback_type: str) -> dict[str, Any]:
    """The effective chain for MODEL: from the proxy when it can answer, else from /router/settings.

    ``GET /fallback/{model}`` 404s for every name holding ``/`` (the path
    parameter does not match a slash) and for a model with no chain; either
    way the chain is resolved locally, never reported as "none" off a 404.
    """
    if fb_mod.path_reachable(model):
        try:
            res = be.get(conn, fb_mod.fallback_path(model), params={"fallback_type": fallback_type}) or {}
        except be.ApiError as e:
            if e.status != 404:
                raise _api_error(e) from e
            reason = f"GET /fallback/{{model}} answered 404 ({e.message})"
        else:
            return {
                "fallback_models": res.get("fallback_models") or [],
                "resolved_by": "proxy",
                "match": None,
                "note": None,
            }
    else:
        reason = "the name contains '/', which GET /fallback/{model} cannot match"
    current = _call(_router_settings, conn).get("current_values") or {}
    local = fb_mod.effective_chain(current.get(fb_mod.FIELDS[fallback_type]), model)
    return {**local, "resolved_by": "router_settings", "reason": reason}


@fallbacks_grp.command("show")
@click.argument("model")
@_fallback_type_option
@click.pass_context
def fallbacks_show(ctx, model, fallback_type):
    """The chain the router would use for MODEL.

    Resolved the router's way: MODEL's own rule, else a provider-prefixed or
    stripped match, else the * catch-all. Asks GET /fallback/{model} when it
    can; for a name containing '/' (which that route cannot match) or a 404,
    the chain is resolved locally from GET /router/settings (resolved_by).
    """
    out = {"model": model, "fallback_type": fallback_type, **_resolve_chain(_conn(ctx), model, fallback_type)}

    def human(r):
        if r["fallback_models"] or r["resolved_by"] == "proxy":
            click.echo(f"  {model} ({fallback_type}): {_chain_text(r['fallback_models'])}")
        else:
            click.echo(f"  {model} ({fallback_type}): no rule in /router/settings applies")
        if r["resolved_by"] != "proxy":
            click.echo(
                f"  resolved from /router/settings ({r['reason']}); match: {r['match'] or 'no rule applies'}"
            )
        if r.get("note"):
            click.echo(f"  note: {r['note']}")

    _emit(ctx, out, human)


@fallbacks_grp.command("set")
@click.argument("model")
@click.option("--to", "to", required=True, help="Fallback model groups in order, comma-separated.")
@_fallback_type_option
@click.pass_context
def fallbacks_set(ctx, model, to, fallback_type):
    """Create or replace MODEL's fallback rule (POST /fallback).

    Every name is checked against the live model groups first. Written to the
    proxy's DB (needs STORE_MODEL_IN_DB) and applied to the worker that
    answered; other workers pick it up from the DB. Not in git: `drift` will
    report it until the config says the same.
    """
    c = _conn(ctx)
    settings = _call(_router_settings, c)
    _refuse_config_owned(settings, fallback_type)
    known = {d.get("model_name") for d in _call(live_deployments, c)}
    try:
        body = fb_mod.new_body(model, to.split(","), fallback_type, known)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    current = settings.get("current_values") or {}
    previous = fb_mod.rules(current.get(fb_mod.FIELDS[fallback_type])).get(body["model"])
    if _dry(ctx, "POST", "/fallback", body):
        return
    res = _call(be.post, c, "/fallback", body) or {}
    out = {**body, "replaced": previous, "message": res.get("message")}
    _emit(
        ctx,
        out,
        lambda r: click.echo(
            f"  {r['model']} ({fallback_type}) → {_chain_text(r['fallback_models'])}"
            + (f"  (was {_chain_text(previous)})" if previous else "")
        ),
    )


@fallbacks_grp.command("delete")
@click.argument("model")
@_fallback_type_option
@click.option("--yes", is_flag=True)
@click.pass_context
def fallbacks_delete(ctx, model, fallback_type, yes):
    """Remove MODEL's fallback rule (DELETE /fallback/{model}).

    Its failures stop failing over (unless a * catch-all rule still covers it,
    which the output names).
    """
    c = _conn(ctx)
    settings = _call(_router_settings, c)
    _refuse_config_owned(settings, fallback_type)
    held = fb_mod.rules((settings.get("current_values") or {}).get(fb_mod.FIELDS[fallback_type]))
    chain = held.get(model)
    if chain is None:
        raise click.ClickException(f"no {fallback_type} fallback rule for {model!r} (see `fallbacks list`)")
    if not fb_mod.path_reachable(model):
        raise click.ClickException(
            f"cannot delete the {fallback_type} rule for {model!r}: DELETE /fallback/{{model}} does not match "
            "a name containing '/' (the proxy answers 404 Not Found, escaped or not), so nothing was deleted. "
            f"Remove it by rewriting router_settings.{fb_mod.FIELDS[fallback_type]} without this rule "
            "(the DB copy: Admin UI router settings or POST /config/update), or, if config.yaml sets that "
            "list, edit the config. `fallbacks set` can only replace the chain, not remove it."
        )
    path = fb_mod.fallback_path(model)
    if _dry(ctx, "DELETE", f"{path}?fallback_type={fallback_type}"):
        return
    _confirm(ctx, yes, f"delete the {fallback_type} fallback rule {model} → {_chain_text(chain)}")
    try:
        be.delete(c, path, params={"fallback_type": fallback_type})
    except be.ApiError as e:
        if e.status == 404 and e.message == fb_mod.ROUTE_NOT_FOUND:
            raise click.ClickException(
                f"DELETE {path} reached no route (404 Not Found): nothing was deleted"
            ) from e
        raise _api_error(e) from e
    out = {
        "deleted": model,
        "fallback_type": fallback_type,
        "fallbacks": chain,
        "still_covered_by": held.get(fb_mod.CATCH_ALL) if model != fb_mod.CATCH_ALL else None,
    }
    _emit(
        ctx,
        out,
        lambda r: click.echo(
            f"  deleted {model} ({fallback_type}) → {_chain_text(chain)}"
            + (
                f"; the * rule still applies: {_chain_text(r['still_covered_by'])}"
                if r["still_covered_by"]
                else ""
            )
        ),
    )


@fallbacks_grp.command("test")
@click.argument("model")
@_fallback_type_option
@click.option("--text", default="Reply with the word ok.", show_default=True, help="The prompt sent.")
@click.pass_context
def fallbacks_test(ctx, model, fallback_type, text):
    """Fail MODEL on purpose and check that its fallback chain serves.

    Sends one REAL chat completion with the router's mock-failure flag: the
    primary is skipped and a fallback answers (logged and billed like any
    request). Verdict failed_over (exit 0), or chain_failed / not_triggered /
    unknown (exit 2). Chat model groups only.
    """
    c = _conn(ctx)
    chain = _resolve_chain(c, model, fallback_type)["fallback_models"]
    if not chain:
        raise click.ClickException(
            f"no {fallback_type} fallbacks apply to {model!r}: nothing to test (see `fallbacks show`)"
        )
    body = fb_mod.probe_body(model, fallback_type, text)
    if _dry(ctx, "POST", "/v1/chat/completions", body):
        return
    deps = _call(live_deployments, c)
    headers: dict[str, str] = {}
    try:
        be.post(c, "/v1/chat/completions", body, response_headers=headers)
    except be.ApiError as e:
        if e.status in (401, 403) or e.status is None:
            raise _api_error(e) from e
        out = {
            "model": model,
            "chain": chain,
            "verdict": "chain_failed",
            "status": e.status,
            "error": health_mod.redact_error(e.message),
        }
    else:
        out = fb_mod.probe_verdict(model, chain, headers, deps)

    def human(r):
        click.echo(f"  {r['verdict']}: {model} ({fallback_type}) → {_chain_text(chain)}")
        if r.get("served_by"):
            click.echo(f"  served by {r['served_by']} ({r['deployment_id']})")
        if r.get("error"):
            click.echo(f"  error {r['status']}: {r['error']}")

    _emit(ctx, out, human)
    if out["verdict"] != "failed_over":
        ctx.exit(2)


@cli.group("guardrails")
def guardrails_grp():
    """Guardrails loaded on the proxy."""


GUARD_COLS = [
    ("name", "NAME"),
    ("source", "SRC"),
    ("guardrail", "CLASS"),
    ("mode", "MODE"),
    ("default_on", "DEFAULT"),
    ("guardrail_id", "ID"),
]


def all_guardrails(conn) -> list[dict]:
    """DB + config guardrails (GET /v2/guardrails/list), each with its definition location."""
    return guard_mod.as_rows(be.get(conn, "/v2/guardrails/list"), "guardrails")


def _resolve_guardrail(conn, ref: str, rows: list[dict] | None = None) -> dict[str, Any]:
    rows = rows if rows is not None else _call(all_guardrails, conn)
    try:
        g = guard_mod.resolve(rows, ref)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if not g:
        raise click.ClickException(f"no guardrail matches {ref!r} — run `guardrails list --all`")
    return g


def _guard_db_only(g: dict[str, Any], verb: str) -> None:
    if guard_mod.location(g) != "db":
        raise click.ClickException(
            f"guardrail {g.get('guardrail_name')} is defined in config.yaml and is read-only through the API — "
            f"{verb} it in the config the proxy is deployed from"
        )


@guardrails_grp.command("list")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="DB guardrails too (GET /v2/guardrails/list), with SRC and id. Default: config.yaml only.",
)
@click.pass_context
def guardrails_list(ctx, show_all):
    """Guardrails on the proxy. Without --all: what config.yaml loaded (what `drift` compares)."""
    if show_all:
        rows = [guard_mod.normalize(g) for g in _call(all_guardrails, _conn(ctx))]
        _emit(ctx, rows, lambda r: _table(r, GUARD_COLS))
        return
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


@guardrails_grp.command("info")
@click.argument("guardrail_ref")
@click.pass_context
def guardrails_info(ctx, guardrail_ref):
    """One guardrail (id or name): params with secrets masked and URLs cut to their origin."""
    c = _conn(ctx)
    g = _resolve_guardrail(c, guardrail_ref)
    gid = g.get("guardrail_id")
    full = _call(be.get, c, f"/guardrails/{quote(str(gid), safe='')}/info") if gid else g
    _emit(ctx, guard_mod.redact(full or g))


def _guardrail_write_options(fn):
    for opt in reversed(
        [
            click.option("--api-base", default=None, help="Guardrail service URL (or os.environ/NAME)."),
            click.option(
                "--api-key", default=None, help="os.environ/NAME only — the proxy reads the variable."
            ),
            click.option("--description", default=None),
            click.option(
                "--param",
                "params",
                multiple=True,
                metavar="KEY=VALUE",
                help="Extra litellm_params (repeatable); true/false/numbers are typed.",
            ),
        ]
    ):
        fn = opt(fn)
    return fn


@guardrails_grp.command("add")
@click.option("--name", required=True, help="guardrail_name: what keys, teams and requests reference.")
@click.option("--guardrail", "integration", required=True, help="Integration, e.g. presidio, lakera_v2.")
@click.option(
    "--mode", required=True, help=f"One or more of: {', '.join(guard_mod.MODES)} (comma-separated)."
)
@click.option("--default-on", is_flag=True, help="Run on every request, not only when asked for.")
@_guardrail_write_options
@click.pass_context
def guardrails_add(ctx, name, integration, mode, default_on, api_base, api_key, description, params):
    """Add a guardrail to the proxy's database (POST /guardrails); it is live at once.

    A DB guardrail survives a config redeploy and is not in git. `--default-on`
    puts it on EVERY request — check it with `guardrails test` first.
    """
    try:
        body = guard_mod.new_guardrail(
            name,
            integration,
            mode,
            default_on=default_on,
            api_base=api_base,
            api_key=api_key,
            description=description,
            params=guard_mod.parse_params(params),
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if _dry(ctx, "POST", "/guardrails", body):
        return
    res = _call(be.post, _conn(ctx), "/guardrails", body) or {}
    out = guard_mod.redact(res) if isinstance(res, dict) else res
    _emit(ctx, out, lambda r: click.echo(f"  added guardrail {name} ({r.get('guardrail_id')})"))


@guardrails_grp.command("update")
@click.argument("guardrail_ref")
@click.option("--name", default=None, help="Rename (keys/teams referencing the old name stop matching).")
@click.option(
    "--default-on/--no-default-on", default=None, help="Run on every request, or only when asked for."
)
@_guardrail_write_options
@click.pass_context
def guardrails_update(ctx, guardrail_ref, name, default_on, api_base, api_key, description, params):
    """Change a DB guardrail in place (PATCH /guardrails/{id}): only the flags given are sent.

    `mode` and the integration cannot be changed in place; delete and re-add.
    """
    c = _conn(ctx)
    g = _resolve_guardrail(c, guardrail_ref)
    _guard_db_only(g, "change")
    try:
        body = guard_mod.patch_body(
            g,
            name=name,
            default_on=default_on,
            api_base=api_base,
            api_key=api_key,
            description=description,
            params=guard_mod.parse_params(params),
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    path = f"/guardrails/{quote(str(g['guardrail_id']), safe='')}"
    if _dry(ctx, "PATCH", path, body):
        return
    res = _call(be.patch, c, path, body) or {}
    _emit(ctx, guard_mod.redact(res) if isinstance(res, dict) else res)


@guardrails_grp.command("delete")
@click.argument("guardrail_ref")
@click.option("--yes", is_flag=True)
@click.pass_context
def guardrails_delete(ctx, guardrail_ref, yes):
    """Remove a DB guardrail (DELETE /guardrails/{id}); requests stop being checked by it at once.

    Keys, teams and requests that still name it are listed (by key alias) so
    the dangling references can be cleaned up.
    """
    c = _conn(ctx)
    g = _resolve_guardrail(c, guardrail_ref)
    _guard_db_only(g, "remove")
    path = f"/guardrails/{quote(str(g['guardrail_id']), safe='')}"
    if _dry(ctx, "DELETE", path):
        return
    gname = g.get("guardrail_name")
    referencing = [
        k.get("key_alias") or k.get("token")
        for k in _call(all_keys, c)
        if gname in (k.get("guardrails") or (k.get("metadata") or {}).get("guardrails") or [])
    ]
    _confirm(ctx, yes, f"delete guardrail {gname} (named by {len(referencing)} key(s))")
    _call(be.delete, c, path)
    _emit(
        ctx,
        {"deleted": g["guardrail_id"], "name": gname, "keys_still_naming_it": referencing},
        lambda d: (
            click.echo(f"deleted guardrail {d['name']} ({d['deleted']})"),
            click.echo("keys still naming it: " + (", ".join(d["keys_still_naming_it"]) or "(none)")),
        ),
    )


@guardrails_grp.command("test")
@click.argument("guardrail_name")
@click.option("--text", default=None, help="Text to check (else read from stdin).")
@click.pass_context
def guardrails_test(ctx, guardrail_name, text):
    """Run ONE guardrail on a text (POST /guardrails/apply_guardrail) and report the verdict.

    `passed` = text unchanged, `modified` = rewritten (e.g. PII masked),
    `blocked` = the guardrail rejected it (exit 3). The call is a real request:
    it is logged, may hit a paid guardrail service, and runs as the CLI's key.
    """
    if text is None:
        if sys.stdin.isatty():
            raise click.ClickException("give --text or pipe the text on stdin")
        text = sys.stdin.read()
    if not text:
        raise click.ClickException("nothing to test: the text is empty")
    body = {"guardrail_name": guardrail_name, "text": text}
    try:
        res = be.post(_conn(ctx), "/guardrails/apply_guardrail", body)
        out = {"guardrail": guardrail_name, **guard_mod.verdict(text, res)}
    except be.ApiError as e:
        if e.status == 404:
            raise click.ClickException(
                f"{e}  (no guardrail by that name is loaded — `guardrails list --all`)"
            ) from e
        if e.status in (401, 403) or e.status is None or e.status >= 500:
            raise click.ClickException(str(e)) from e
        out = {"guardrail": guardrail_name, "verdict": "blocked", "status": e.status, "reason": e.message}
    _emit(
        ctx,
        out,
        lambda d: click.echo(
            f"  {d['guardrail']}: {d['verdict']}"
            + (f" — {d.get('reason')}" if d.get("reason") else "")
            + (f"\n  -> {d['response_text']}" if d["verdict"] == "modified" else "")
        ),
    )
    if out["verdict"] == "blocked":
        ctx.exit(3)


def _usage_window(days, start, end) -> dict[str, str]:
    try:
        return guard_mod.window(days, start, end)
    except ValueError as e:
        raise click.ClickException(str(e)) from e


@guardrails_grp.command("usage")
@click.argument("guardrail_ref", required=False)
@click.option("--days", default=7, type=int, help="Window ending today (the proxy's own default is 7).")
@click.option("--start", default=None, help="YYYY-MM-DD; with --end, overrides --days.")
@click.option("--end", default=None, help="YYYY-MM-DD.")
@click.pass_context
def guardrails_usage(ctx, guardrail_ref, days, start, end):
    """How often each guardrail ran, blocked and failed (GET /guardrails/usage/overview).

    With a guardrail: its daily series and per-team / per-key units
    (/guardrails/usage/detail/{id}). From the proxy's daily metrics tables —
    no spend-log download. Exit 2 if any guardrail is `critical` (>15% blocked).
    """
    c = _conn(ctx)
    win = _usage_window(days, start, end)
    if guardrail_ref:
        g = _resolve_guardrail(c, guardrail_ref)
        res = (
            _call(be.get, c, f"/guardrails/usage/detail/{quote(str(g['guardrail_id']), safe='')}", params=win)
            or {}
        )
        keep = (
            "guardrail_id",
            "guardrail_name",
            "provider",
            "requestsEvaluated",
            "failRate",
            "status",
            "trend",
        )
        data = {
            "window": win,
            **{k: res.get(k) for k in keep},
            "time_series": res.get("time_series") or [],
            "usage_units_by_team": res.get("usage_units_by_team") or {},
            "cost": res.get("cost"),
        }
        _emit(
            ctx,
            data,
            lambda d: (
                click.echo(
                    f"  {d['guardrail_name']}: {d['requestsEvaluated']} evaluated, {d['failRate']}% blocked "
                    f"({d['status']}, trend {d['trend']}) {d['window']['start_date']}..{d['window']['end_date']}"
                ),
                _table(d["time_series"], [("date", "DATE"), ("passed", "PASSED"), ("blocked", "BLOCKED")]),
            ),
        )
        if data["status"] == "critical":
            ctx.exit(2)
        return
    res = _call(be.get, c, "/guardrails/usage/overview", params=win) or {}
    rows = guard_mod.usage_rows(res)
    critical = [r["name"] for r in rows if r["status"] == "critical"]
    data = {"window": win, **guard_mod.usage_totals(res), "critical": critical, "rows": rows}
    _emit(
        ctx,
        data,
        lambda d: (
            click.echo(
                f"  {d['requests']} evaluated, {d['blocked']} blocked, pass rate {d['pass_rate']}% "
                f"({d['window']['start_date']}..{d['window']['end_date']})"
            ),
            _table(
                d["rows"],
                [
                    ("name", "GUARDRAIL"),
                    ("provider", "PROVIDER"),
                    ("requests", "EVALUATED"),
                    ("fail_rate", "BLOCKED%"),
                    ("status", "STATUS"),
                    ("trend", "TREND"),
                ],
            ),
        ),
    )
    if critical:
        ctx.exit(2)


@guardrails_grp.command("logs")
@click.argument("guardrail_ref")
@click.option("--action", type=click.Choice(guard_mod.ACTIONS), default=None)
@click.option("--days", default=7, type=int)
@click.option("--start", default=None, help="YYYY-MM-DD; with --end, overrides --days.")
@click.option("--end", default=None)
@click.option("--limit", default=50, type=int, help=f"Rows (one page, at most {guard_mod.LOGS_PAGE_MAX}).")
@click.option("--page", default=1, type=int)
@click.option("--snippets", is_flag=True, help="Include prompt/response snippets (user content).")
@click.pass_context
def guardrails_logs(ctx, guardrail_ref, action, days, start, end, limit, page, snippets):
    """The requests one guardrail judged (GET /guardrails/usage/logs), newest first.

    Paged server-side through the guardrail index (never a raw spend-log
    download). Prompt/response snippets are left out unless --snippets.
    """
    if not 1 <= limit <= guard_mod.LOGS_PAGE_MAX:
        raise click.ClickException(f"--limit must be 1..{guard_mod.LOGS_PAGE_MAX}")
    if page < 1:
        raise click.ClickException("--page must be at least 1")
    c = _conn(ctx)
    win = _usage_window(days, start, end)
    g = _resolve_guardrail(c, guardrail_ref)
    params = {"guardrail_id": g["guardrail_id"], "page": page, "page_size": limit, **win}
    if action:
        params["action"] = action
    res = _call(be.get, c, "/guardrails/usage/logs", params=params) or {}
    rows = guard_mod.log_rows(res, snippets)
    data = {
        "guardrail": g.get("guardrail_name"),
        "total": res.get("total"),
        "page": res.get("page"),
        "rows": rows,
    }
    _emit(
        ctx,
        data,
        lambda d: (
            click.echo(f"  {d['guardrail']}: {d['total']} run(s) in the window, page {d['page']}"),
            _table(
                d["rows"],
                [
                    ("timestamp", "WHEN"),
                    ("action", "ACTION"),
                    ("model", "MODEL"),
                    ("latency_ms", "LATENCY(ms)"),
                    ("reason", "REASON"),
                ],
            ),
        ),
    )


@guardrails_grp.command("submissions")
@click.option("--status", type=click.Choice(["pending_review", "active", "rejected"]), default=None)
@click.option("--team", default=None, help="Only this team_id.")
@click.pass_context
def guardrails_submissions(ctx, status, team):
    """Team-registered guardrails awaiting review (GET /guardrails/submissions)."""
    params = {k: v for k, v in {"status": status, "team_id": team}.items() if v}
    res = _call(be.get, _conn(ctx), "/guardrails/submissions", params=params or None) or {}
    data = {"summary": res.get("summary") or {}, "submissions": guard_mod.submission_rows(res)}
    _emit(
        ctx,
        data,
        lambda d: (
            click.echo("  " + ", ".join(f"{k} {v}" for k, v in d["summary"].items())),
            _table(
                d["submissions"],
                [
                    ("name", "GUARDRAIL"),
                    ("status", "STATUS"),
                    ("team_id", "TEAM"),
                    ("api_base", "SERVICE"),
                    ("submitted_by", "BY"),
                    ("guardrail_id", "ID"),
                ],
            ),
        ),
    )


_SUBMISSION_HELP = {
    "approve": "Approve a pending team submission: it becomes active and runs at once (--yes off a TTY).",
    "reject": "Reject a pending team submission.",
}


def _submission_action(verb: str):
    @guardrails_grp.command(verb, help=_SUBMISSION_HELP[verb])
    @click.argument("guardrail_id")
    @click.option("--yes", is_flag=True)
    @click.pass_context
    def _cmd(ctx, guardrail_id, yes):
        path = f"/guardrails/submissions/{quote(guardrail_id, safe='')}/{verb}"
        if _dry(ctx, "POST", path):
            return
        if verb == "approve":
            # Approval loads a team-supplied guardrail into the live proxy.
            _confirm(ctx, yes, f"approve guardrail submission {guardrail_id} (it goes live at once)")
        _emit(ctx, _call(be.post, _conn(ctx), path, None))

    return _cmd


_submission_action("approve")
_submission_action("reject")


# ── keys ────────────────────────────────────────────────────────────────────

KEY_COLS = [
    ("key_alias", "ALIAS"),
    ("key_name", "KEY"),
    ("team_id", "TEAM"),
    ("user_id", "USER"),
    ("tags", "TAGS"),
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
        "tags": ", ".join(tags_mod.key_tags(k))
        or None,  # metadata.tags (the proxy flat-maps `tags` on older shapes)
        "mcp": mcp_mod.key_mcp(k),  # object_permission's MCP grant (see `mcp access`)
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


def _mcp_grant(ctx, groups_raw: str | None, servers_raw: str | None) -> dict[str, Any] | None:
    """object_permission for --mcp-groups/--mcp-servers, validated against the live proxy."""
    if groups_raw is None and servers_raw is None:
        return None
    c = _conn(ctx)
    servers = _call(all_mcp_servers, c) if servers_raw is not None else []
    groups = _call(mcp_groups, c) if groups_raw is not None else []
    try:
        return mcp_mod.grant_permission(servers, groups, groups_raw, servers_raw)
    except ValueError as e:
        raise click.ClickException(str(e)) from e


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
@click.option(
    "--tags",
    default=None,
    help="Comma-separated tags, e.g. feat-x,team-a — the labels `spend --by tag` bills by.",
)
@click.option(
    "--mcp-groups",
    default=None,
    help="Comma-separated MCP access groups the key may use (`mcp groups`); '' clears.",
)
@click.option(
    "--mcp-servers",
    default=None,
    help="Comma-separated MCP servers (id, alias or name; `mcp list`); 'none' = no MCP at all.",
)
@click.pass_context
def keys_generate(
    ctx, alias, team, user, models, duration, max_budget, budget_id, rpm, tpm, tags, mcp_groups, mcp_servers
):
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
    if tags:
        try:
            body["metadata"] = tags_mod.as_metadata(None, tags_mod.parse_tags(tags))
        except ValueError as e:
            raise click.ClickException(str(e)) from e
    perm = _mcp_grant(ctx, mcp_groups, mcp_servers)
    if perm is not None:
        body["object_permission"] = perm
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
@click.option(
    "--tags",
    default=None,
    help="Comma-separated tags, e.g. feat-x,team-a (replaces the tag set; other metadata is kept).",
)
@click.option(
    "--mcp-groups",
    default=None,
    help="Comma-separated MCP access groups the key may use (`mcp groups`); '' clears.",
)
@click.option(
    "--mcp-servers",
    default=None,
    help="Comma-separated MCP servers (id, alias or name; `mcp list`); 'none' = no MCP at all.",
)
@click.pass_context
def keys_update(
    ctx, key_or_alias, models, max_budget, budget_id, rpm, tpm, duration, tags, mcp_groups, mcp_servers
):
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
    want_tags = None
    if tags:
        try:
            want_tags = tags_mod.parse_tags(tags)
        except ValueError as e:
            raise click.ClickException(str(e)) from e
    perm = _mcp_grant(ctx, mcp_groups, mcp_servers)
    if perm is not None:
        # The proxy merges object_permission per field, so the key's other
        # grants (vector stores, agents, the MCP field not given) are kept.
        body["object_permission"] = perm
    if not body and not want_tags:
        raise click.ClickException("nothing to update")
    body["key"] = _call(_resolve_token, c, key_or_alias)
    if want_tags:
        k = next((k for k in _call(all_keys, c) if k.get("token") == body["key"]), {})
        # /key/update replaces the whole metadata object: merge over the key's
        # existing metadata so a retag does not silently drop other fields.
        body["metadata"] = tags_mod.as_metadata(k.get("metadata"), want_tags)
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
    perm = mcp_mod.permission_body(old.get("object_permission"))
    if perm:  # MCP / vector-store / agent grants live in a separate row: copy them, not the id
        new_body["object_permission"] = perm
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


# ── customers ───────────────────────────────────────────────────────────────


CUSTOMER_COLS = [
    ("user_id", "USER_ID"),
    ("alias", "ALIAS"),
    ("spend", "SPEND"),
    ("max_budget", "BUDGET"),
    ("budget_id", "BUDGET_ID"),
    ("tpm_limit", "TPM"),
    ("rpm_limit", "RPM"),
    ("blocked", "BLOCKED"),
]

CUSTOMERS_PAGE = 100


@cli.group("customers")
def customers_grp():
    """End users of the app behind the proxy: per-user spend caps and blocks.

    A customer is identified by the `user_id` apps pass with each request —
    spend is tracked against it live, capped by `--max-budget` (inline or a
    shared `--budget-id`), and stops the moment `customers block` is run.
    """


def all_customers(conn, budget: str | None = None) -> list[dict]:
    """Every customer, following /customer/list pagination (same loop as keys/users)."""
    out, page = [], 1
    while True:
        res = be.get(conn, "/customer/list", params={"page": page, "size": CUSTOMERS_PAGE}) or {}
        rows = customers_mod.as_rows(res)  # a bare list (older proxies) has no page envelope
        if budget:
            rows = [c for c in rows if c.get("budget_id") == budget]
        out += rows
        total = (res.get("total_pages") or 1) if isinstance(res, dict) else 1
        if page >= total:
            return out
        page += 1


def _resolve_customer(conn, ref: str) -> dict[str, Any]:
    """The live customer record for `ref` (user_id or unique alias), or a Click error."""
    t = customers_mod.resolve_customer_target(_call(all_customers, conn), ref)
    if not t:
        raise click.ClickException(f"no live customer matches {ref!r} — run `customers list`")
    return t["customer"]


@customers_grp.command("list")
@click.option("--budget", default=None, help="Only customers on this shared budget id.")
@click.pass_context
def customers_list(ctx, budget):
    rows = [customers_mod.normalize(c) for c in _call(all_customers, _conn(ctx), budget)]
    _emit(ctx, rows, lambda r: _table(r, CUSTOMER_COLS))


@customers_grp.command("info")
@click.argument("customer_ref")
@click.pass_context
def customers_info(ctx, customer_ref):
    """By user id or unique alias, straight from /customer/info."""
    c = _conn(ctx)
    # /customer/info is keyed by user_id; an alias is resolved to one first.
    user_id = customers_mod.resolve_customer_target(_call(all_customers, c), customer_ref)
    user_id = user_id["matches"][0] if user_id else customer_ref
    res = _call(be.get, c, "/customer/info", params={"user_id": user_id})
    _emit(
        ctx, res, lambda r: _table([customers_mod.normalize(r)] if isinstance(r, dict) else r, CUSTOMER_COLS)
    )


@customers_grp.command("create")
@click.option("--user-id", default=None, help="What the app passes as user_id on each request.")
@click.option("--alias", default=None, help="Human label shown in tables and spend reports.")
@click.option("--max-budget", type=float, default=None, help="Hard spend cap per reset period.")
@click.option("--budget-id", "budget_id", default=None, help="Attach a shared budget (`budgets create`).")
@click.option("--rpm", type=int, default=None)
@click.option("--tpm", type=int, default=None)
@click.pass_context
def customers_create(ctx, user_id, alias, max_budget, budget_id, rpm, tpm):
    """POST /customer/new. Apps pass `user_id` with requests to be capped and tracked."""
    try:
        body = customers_mod.new_customer(user_id, alias, max_budget, budget_id, rpm, tpm)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if _dry(ctx, "POST", "/customer/new", body):
        return
    res = _call(be.post, _conn(ctx), "/customer/new", body)
    _emit(
        ctx,
        res,
        lambda r: click.echo(
            f"  created {(r.get('user_id') if isinstance(r, dict) else None) or user_id or alias}"
        ),
    )


@customers_grp.command("update")
@click.argument("customer_ref")
@click.option("--alias", default=None)
@click.option("--max-budget", type=float, default=None)
@click.option(
    "--budget-id",
    "budget_id",
    default=None,
    help="Shared budget (`budgets create`) — overrides --max-budget.",
)
@click.option("--rpm", type=int, default=None)
@click.option("--tpm", type=int, default=None)
@click.pass_context
def customers_update(ctx, customer_ref, alias, max_budget, budget_id, rpm, tpm):
    """POST /customer/update. Given flags replace the field; omitted ones stay as-is."""
    c = _conn(ctx)
    user_id = _resolve_customer(c, customer_ref)["user_id"]
    try:
        body = customers_mod.update_customer(
            user_id, alias=alias, max_budget=max_budget, budget_id=budget_id, rpm=rpm, tpm=tpm
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if _dry(ctx, "POST", "/customer/update", body):
        return
    _emit(ctx, _call(be.post, c, "/customer/update", body))


def _simple_customer_action(name: str, path: str, destructive: bool):
    @customers_grp.command(name)
    @click.argument("customer_ref")
    @click.option("--yes", is_flag=True)
    @click.pass_context
    def _cmd(ctx, customer_ref, yes):
        c = _conn(ctx)
        user_id = _resolve_customer(c, customer_ref)["user_id"]
        body = customers_mod.ids_body([user_id])
        if _dry(ctx, "POST", path, body):
            return
        if destructive:
            _confirm(ctx, yes, f"{name} customer {customer_ref}")
        _emit(ctx, _call(be.post, c, path, body), lambda r: click.echo(f"  {name}ed customer {user_id}"))

    _cmd.__doc__ = {
        "block": "Block a customer (every key request that names their user_id fails); reversible with unblock.",
        "unblock": "Unblock a customer.",
    }[name]
    return _cmd


_simple_customer_action("block", "/customer/block", True)
_simple_customer_action("unblock", "/customer/unblock", False)


@customers_grp.command("delete")
@click.argument("customer_ref")
@click.option("--yes", is_flag=True)
@click.pass_context
def customers_delete(ctx, customer_ref, yes):
    """POST /customer/delete. Stops spend attribution for the user; deletes no key."""
    c = _conn(ctx)
    user_id = _resolve_customer(c, customer_ref)["user_id"]
    body = customers_mod.ids_body([user_id])
    if _dry(ctx, "POST", "/customer/delete", body):
        return
    _confirm(ctx, yes, f"delete customer {customer_ref}")
    _call(be.post, c, "/customer/delete", body)
    _emit(ctx, {"deleted": body}, None)


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


def _users_page(conn, page: int) -> dict:
    """One page of users. v1.106.3 has only GET /user/list (``get_users`` in
    internal_user_endpoints.py: ``page``/``page_size`` ≤ 100, response
    ``UserListResponse``); /user/get_users is what older proxies served and
    404s there, so it is tried first only for those and abandoned on 404/405."""
    try:
        return be.get(conn, "/user/get_users", params={"page": page, "size": USERS_PAGE}) or {}
    except be.ApiError as e:
        if e.status not in (404, 405):
            raise
    return be.get(conn, "/user/list", params={"page": page, "page_size": USERS_PAGE}) or {}


def all_users(conn, team: str | None = None, role: str | None = None) -> list[dict]:
    """Every user, following pagination (same loop as keys)."""
    out, page = [], 1
    while True:
        res = _users_page(conn, page)
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


# ── logs ────────────────────────────────────────────────────────────────────


LOG_COLS = [
    ("when", "WHEN"),
    ("model", "MODEL"),
    ("caller", "CALLER"),
    ("user", "USER"),
    ("call_type", "CALL"),
    ("total_tokens", "TOKENS"),
    ("spend", "SPEND"),
    ("latency_ms", "LATENCY(ms)"),
    ("cache_hit", "CACHE"),
]

SUMMARY_COLS = [
    ("model", "MODEL"),
    ("requests", "REQUESTS"),
    ("spend", "SPEND"),
    ("prompt_tokens", "PROMPT"),
    ("completion_tokens", "COMPLETION"),
    ("latency_ms", "LATENCY p50/p95(ms)"),
]


LOG_PAGE_SIZE = 100


def _fetch_logs(conn, days: int) -> list[dict[str, Any]]:
    """The proxy's spend log over the last ``days`` days (the window `spend` uses).

    Paged through ``/spend/logs/v2``, which leaves out the prompt and response
    columns. The old unpaged ``/spend/logs?summarize=false`` loads every row,
    prompts included, into the proxy worker: a 3-day window OOM-killed both
    pods of a 2Gi proxy (v1.106.3). It also silently truncates to its newest
    rows (header ``x-litellm-spend-logs-truncated``). It is used only when the
    proxy has no v2 route.
    """
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=days + 1)
    window = {"start_date": f"{start.isoformat()} 00:00:00", "end_date": f"{end.isoformat()} 00:00:00"}
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        try:
            res = be.get(conn, "/spend/logs/v2", params={**window, "page": page, "page_size": LOG_PAGE_SIZE})
        except be.ApiError as e:
            if e.status != 404 or page > 1:
                raise
            legacy = _call(
                be.get,
                conn,
                "/spend/logs",
                params={"start_date": start.isoformat(), "end_date": end.isoformat(), "summarize": "false"},
            )
            return legacy if isinstance(legacy, list) else []
        if isinstance(res, list):  # a single unpaged answer
            return res
        rows.extend((res or {}).get("data") or [])
        if page >= int((res or {}).get("total_pages") or 1):
            return rows
        page += 1


def _aliases(conn) -> dict[str, str]:
    return logs_mod.key_alias_map(_call(all_keys, conn), conn.get("key"))


def _log_filters(key, user, team, model, tag) -> dict[str, str]:
    """The active filters, for the output header."""
    return {
        k: v for k, v in {"key": key, "user": user, "team": team, "model": model, "tag": tag}.items() if v
    }


@cli.group("logs")
def logs_grp():
    """Recent requests from the spend log: who called what, tokens, spend, latency."""


@logs_grp.command("list")
@click.option("--key", default=None, help="Key sk-…, hashed token, or alias (`keys list` names them).")
@click.option("--user", default=None, help="end-user / user_id the request carried.")
@click.option("--team", default=None, help="team_id the calling key belongs to.")
@click.option("--model", default=None, help="Model group.")
@click.option("--tag", default=None, help="A tag the request carried (see `spend --by tag`).")
@click.option("--days", default=7, type=int, help="Window in days (the log is written async).")
@click.option("--limit", default=25, type=int, help="Newest N requests to show after filtering.")
@click.pass_context
def logs_list(ctx, key, user, team, model, tag, days, limit):
    """One row per request, newest first — the drill-down behind `spend` totals.

    Logs are written asynchronously: right after traffic nothing is there yet.
    """
    c = _conn(ctx)
    token = None
    if key:
        try:
            token = logs_mod.token_of(key, _call(all_keys, c))
        except ValueError as e:
            raise click.ClickException(str(e)) from e
    rows = logs_mod.filter_logs(_call(_fetch_logs, c, days), key=token, user=user, team=team, model=model)
    if tag:
        rows = tags_mod.filter_by_tags(rows, tag)
    aliases = _aliases(c)
    matched = len(rows)
    rows = [{**logs_mod.normalize(r, aliases), "tags": tags_mod.log_row_tags(r) or None} for r in rows]
    rows.sort(key=lambda r: r["when"] or "", reverse=True)
    rows = rows[: max(limit, 0)]
    data = {
        "window_days": days,
        "filters": _log_filters(key, user, team, model, tag),
        "matched": matched,
        "rows": rows,
    }
    _emit(
        ctx,
        data,
        lambda d: (
            click.echo(
                f"{d['matched']} request(s) in the last {d['window_days']} day(s)"
                + (f", {', '.join(f'{k} {v}' for k, v in d['filters'].items())}" if d["filters"] else "")
                + f" — newest {len(d['rows'])} shown"
            ),
            _table(d["rows"], LOG_COLS),
        ),
    )


@logs_grp.command("summary")
@click.option("--key", default=None, help="Key sk-…, hashed token, or alias.")
@click.option("--user", default=None, help="end-user / user_id the request carried.")
@click.option("--team", default=None, help="team_id the calling key belongs to.")
@click.option("--model", default=None, help="Model group.")
@click.option("--tag", default=None, help="A tag the request carried (see `spend --by tag`).")
@click.option("--days", default=7, type=int, help="Window in days (the log is written async).")
@click.pass_context
def logs_summary(ctx, key, user, team, model, tag, days):
    """Traffic and latency over the window: totals, p50/p95, per-model breakdown.

    `spend` is the money view (grouped by who to bill); this is the traffic
    view over the same log — what share of requests each model takes, whether
    a latency tail is forming, how often the cache answered.
    """
    c = _conn(ctx)
    token = None
    if key:
        try:
            token = logs_mod.token_of(key, _call(all_keys, c))
        except ValueError as e:
            raise click.ClickException(str(e)) from e
    rows = logs_mod.filter_logs(_call(_fetch_logs, c, days), key=token, user=user, team=team, model=model)
    if tag:
        rows = tags_mod.filter_by_tags(rows, tag)
    res = logs_mod.summary(rows)
    res["window_days"] = days
    res["filters"] = _log_filters(key, user, team, model, tag)
    _emit(
        ctx,
        res,
        lambda r: (
            click.echo(
                f"{r['requests']} request(s) in the last {r['window_days']} day(s): "
                f"${r['spend']} · cache {r['cache_hits']} hit(s)"
            ),
            click.echo(
                "latency p50/p95/max: "
                + " / ".join(f"{v}ms" if v is not None else "-" for v in r["latency_ms"].values())
            ),
            _table(
                [
                    {**m, **{"latency_ms": f"{m['latency_ms']['p50']}/{m['latency_ms']['p95']}"}}
                    for m in r["models"]
                ],
                SUMMARY_COLS,
            ),
        ),
    )


# ── spend ───────────────────────────────────────────────────────────────────


@cli.command("spend")
@click.option("--days", default=7, type=int)
@click.option(
    "--by",
    "group_by",
    type=click.Choice(["model", "key", "team", "user", "tag"]),
    default="model",
)
@click.pass_context
def spend(ctx, days, group_by):
    """Spend and request totals over the last N days, grouped client-side from /spend/logs.

    `--by tag` is the chargeback view: every key's `--tags` (see
    `keys generate --tags`) ride along with its requests, so a request is
    counted under each tag it carried; requests with no tag land in
    `(untagged)`.
    """
    c = _conn(ctx)
    logs = _call(_fetch_logs, c, days)
    logs = logs if isinstance(logs, list) else []
    if group_by == "tag":
        data = {
            "days": days,
            "group_by": group_by,
            "log_rows": len(logs),
            "rows": tags_mod.spend_by_tag(logs),
        }
        _emit(
            ctx,
            data,
            lambda d: _table(
                d["rows"],
                [
                    ("tag", "TAG"),
                    ("requests", "REQUESTS"),
                    ("spend", "SPEND"),
                    ("prompt_tokens", "PROMPT"),
                    ("completion_tokens", "COMPLETION"),
                ],
            ),
        )
        return
    field = {"model": "model_group", "key": "api_key", "team": "team_id", "user": "user"}[group_by]
    agg: dict[str, dict[str, Any]] = {}
    alias: dict[str, str] = {}
    if group_by == "key":
        alias = {k.get("token"): k.get("key_alias") or k.get("key_name") for k in _call(all_keys, c)}
        if c.get("key"):  # LiteLLM stores sha256(key); name the one we authenticate with
            alias[hashlib.sha256(c["key"].encode()).hexdigest()] = "(master key)"
        alias[logs_mod.MASTER_KEY_LITERAL] = "(master key)"  # how newer proxies (v1.106.3) log it
    for row in logs:
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
        "log_rows": len(logs),
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


# ── usage (server-side spend reports) ──────────────────────────────────────


def _usage_cols(group_by: str) -> list[tuple[str, str]]:
    if group_by == "day":
        return [
            ("date", "DATE"),
            ("spend", "SPEND"),
            ("prompt_tokens", "PROMPT"),
            ("completion_tokens", "COMPLETION"),
        ]
    return [
        ("date", "DATE"),
        (group_by, group_by.upper()),
        ("spend", "SPEND"),
        ("prompt_tokens", "PROMPT"),
        ("completion_tokens", "COMPLETION"),
    ]


@cli.group("usage")
def usage_grp():
    """Spend reports the proxy aggregates in its own database (/global/spend/report)."""


@usage_grp.command("report")
@click.option(
    "--by",
    "group_by",
    type=click.Choice(usage_mod.DIMENSIONS),
    default="day",
    help="Day totals, or per-team / per-key / per-model / per-end-user rows. Default: day.",
)
@click.option("--days", default=7, type=int, help="Window in days (used without --start/--end).")
@click.option("--start", default=None, help="Window start, YYYY-MM-DD. Requires --end; overrides --days.")
@click.option("--end", default=None, help="Window end (inclusive), YYYY-MM-DD. Requires --start.")
@click.option("--limit", default=None, type=int, help="Newest N rows to show; default all.")
@click.pass_context
def usage_report(ctx, group_by, days, start, end, limit):
    """Spend and token totals over a window — aggregated on the proxy, not downloaded.

    `spend` and `logs summary` aggregate raw log rows client-side, so a wide
    window means downloading every row. This asks the proxy's own aggregation
    (the numbers behind the Admin UI's charts) instead, for windows the log
    download could not carry. There is no per-request drill-down from here:
    narrow the window and use `logs list` for that.
    """
    c = _conn(ctx)
    try:
        win = usage_mod.window(days, start, end)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    names: dict[str, str] = {}
    if group_by == "api_key":
        names = _aliases(c)  # hash → alias, same naming as `spend --by key`
    res = _call(be.get, c, "/global/spend/report", params=usage_mod.params(win, group_by))
    rows = usage_mod.normalize(usage_mod.as_rows(res), group_by, names)
    if limit is not None and limit >= 0:
        rows = rows[:limit]
    tot = usage_mod.totals(rows)
    data = {"window": win, "group_by": group_by, **tot, "rows": rows}

    def human(d):
        click.echo(
            f"spend {d['window']['start_date']} .. {d['window']['end_date']} by {d['group_by']}: "
            f"${d['spend']} over {d['days']} day(s)"
        )
        _table(d["rows"], _usage_cols(group_by))

    _emit(ctx, data, human)


# ── mcp (the proxy's MCP gateway) ──────────────────────────────────────────


MCP_COLS = [
    ("name", "SERVER"),
    ("source", "SRC"),
    ("transport", "TRANSPORT"),
    ("auth_type", "AUTH"),
    ("url", "URL"),
    ("access_groups", "GROUPS"),
    ("status", "STATUS"),
]


def all_mcp_servers(conn) -> list[dict]:
    """The proxy's MCP servers, config- and DB-defined (GET /v1/mcp/server)."""
    return mcp_mod.as_rows(be.get(conn, "/v1/mcp/server"))


def mcp_health(conn, server_ids: list[str] | None = None) -> dict[str, str | None]:
    """server_id → status from GET /v1/mcp/server/health (connects to each server)."""
    params = {"server_ids": server_ids} if server_ids else None
    res = be.get(conn, "/v1/mcp/server/health", params=params)
    return {r.get("server_id"): r.get("status") for r in mcp_mod.as_rows(res)}


def mcp_groups(conn) -> list[str]:
    return list((be.get(conn, "/v1/mcp/access_groups") or {}).get("access_groups") or [])


def _resolve_mcp(conn, ref: str, servers: list[dict] | None = None) -> dict[str, Any]:
    servers = servers if servers is not None else _call(all_mcp_servers, conn)
    try:
        hit = mcp_mod.resolve_server(servers, ref)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if not hit:
        raise click.ClickException(f"no MCP server matches {ref!r} — run `mcp list`")
    return hit["server"]


def _mcp_db_only(s: dict[str, Any], verb: str) -> None:
    if s.get("is_config"):
        raise click.ClickException(
            f"MCP server {mcp_mod.server_label(s)} is defined in config.yaml (is_config) and is read-only "
            f"through the API — {verb} it in the config the proxy is deployed from"
        )


def _list_str(row: dict, *fields: str) -> dict:
    return {**row, **{f: ",".join(row[f]) if isinstance(row.get(f), list) else row.get(f) for f in fields}}


@cli.group("mcp")
def mcp_grp():
    """MCP servers the proxy fronts: list, health, tools, access, add/update/delete, sessions.

    Servers come from config.yaml (SRC=config, read-only here) or the proxy's
    database (SRC=db). Keys reach them through `object_permission`
    (`keys generate|update --mcp-groups/--mcp-servers`).
    """


@mcp_grp.command("list")
@click.option(
    "--health", "with_health", is_flag=True, help="Also health-check every server (connects to each)."
)
@click.pass_context
def mcp_list(ctx, with_health):
    """Every MCP server: name, source, transport, auth, URL origin, access groups.

    The proxy filters this list by the calling key's own MCP access: run it
    with an admin key for the whole picture (`mcp groups` lists every group
    even when the key sees none of its servers).
    """
    c = _conn(ctx)
    servers = _call(all_mcp_servers, c)
    status = _call(mcp_health, c) if with_health else {}
    rows = [mcp_mod.normalize(s, status.get(s.get("server_id"))) for s in servers]
    _emit(ctx, rows, lambda r: _table([_list_str(x, "access_groups") for x in r], MCP_COLS))


@mcp_grp.command("info")
@click.argument("server_ref")
@click.pass_context
def mcp_info(ctx, server_ref):
    """One server's full record (live-health-checked by the proxy), secrets redacted.

    SERVER_REF is a server_id, alias or server_name. URLs are cut to their
    origin, header/env values masked, and credentials listed by field name
    only: an upstream key can hide in any of them.
    """
    c = _conn(ctx)
    s = _resolve_mcp(c, server_ref)
    full = _call(be.get, c, mcp_mod.server_path(s["server_id"])) or s
    _emit(ctx, mcp_mod.redact_server(full))


@mcp_grp.command("health")
@click.argument("server_refs", nargs=-1)
@click.pass_context
def mcp_health_cmd(ctx, server_refs):
    """Health of every server (or the ones named). Exit 2 if any is unhealthy."""
    c = _conn(ctx)
    servers = _call(all_mcp_servers, c)
    picked = [_resolve_mcp(c, r, servers) for r in server_refs]
    ids = [s["server_id"] for s in picked] or None
    status = _call(mcp_health, c, ids)
    names = {s.get("server_id"): mcp_mod.server_label(s) for s in servers}
    rows = [{"server_id": sid, "name": names.get(sid, sid), "status": st} for sid, st in status.items()]
    rows.sort(key=lambda r: (r["status"] != "unhealthy", r["name"] or ""))
    unhealthy = [r["name"] for r in rows if r["status"] == "unhealthy"]
    data = {"healthy": not unhealthy, "unhealthy": unhealthy, "servers": rows}
    _emit(
        ctx,
        data,
        lambda d: (
            click.echo(f"  {len(d['servers'])} server(s), {len(d['unhealthy'])} unhealthy"),
            _table(d["servers"], [("name", "SERVER"), ("status", "STATUS"), ("server_id", "ID")]),
        ),
    )
    if unhealthy:
        ctx.exit(2)


@mcp_grp.command("tools")
@click.option("--server", "server_ref", default=None, help="Only this server's tools (id, alias or name).")
@click.pass_context
def mcp_tools(ctx, server_ref):
    """Tools the CALLING key can see through the gateway, by server.

    Runs as whatever key the CLI authenticates with: to audit what another
    key reaches, use `mcp access`. read_only/destructive are the server's own
    annotations (hints, not enforced by the proxy).
    """
    c = _conn(ctx)
    servers = _call(all_mcp_servers, c)
    labels = [mcp_mod.server_label(s) for s in servers]
    tools = [
        mcp_mod.normalize_tool(t, labels) for t in mcp_mod.as_rows(_call(be.get, c, "/v1/mcp/tools"), "tools")
    ]
    if server_ref:
        want = mcp_mod.server_label(_resolve_mcp(c, server_ref, servers))
        tools = [t for t in tools if t["server"] == want]
    per: dict[str, int] = {}
    for t in tools:
        per[t["server"] or "(unprefixed)"] = per.get(t["server"] or "(unprefixed)", 0) + 1
    data = {"count": len(tools), "by_server": dict(sorted(per.items())), "tools": tools}
    _emit(
        ctx,
        data,
        lambda d: (
            click.echo(
                f"  {d['count']} tool(s): " + ", ".join(f"{k} {v}" for k, v in d["by_server"].items())
            ),
            _table(
                d["tools"],
                [("server", "SERVER"), ("tool", "TOOL"), ("read_only", "RO"), ("description", "DESCRIPTION")],
            ),
        ),
    )


@mcp_grp.command("groups")
@click.pass_context
def mcp_groups_cmd(ctx):
    """MCP access groups and the servers in each (what `--mcp-groups` grants)."""
    c = _conn(ctx)
    groups = _call(mcp_groups, c)
    servers = _call(all_mcp_servers, c)
    rows = [
        {
            "group": g,
            "servers": sorted(
                mcp_mod.server_label(s) for s in servers if g in (s.get("mcp_access_groups") or [])
            ),
        }
        for g in groups
    ]
    open_to_all = sorted(mcp_mod.server_label(s) for s in servers if s.get("allow_all_keys"))
    data = {"groups": rows, "allow_all_keys": open_to_all}
    _emit(
        ctx,
        data,
        lambda d: (
            _table(
                [_list_str(r, "servers") for r in d["groups"]], [("group", "GROUP"), ("servers", "SERVERS")]
            ),
            click.echo(f"  every key (allow_all_keys): {', '.join(d['allow_all_keys']) or '(none)'}"),
        ),
    )


@mcp_grp.command("access")
@click.option("--server", "server_ref", default=None, help="Only keys whose own grant reaches this server.")
@click.option("--team", default=None, help="Only keys of this team_id.")
@click.pass_context
def mcp_access(ctx, server_ref, team):
    """Which keys reach which MCP servers, from each key's own object_permission.

    `reaches` is groups ∪ named servers ∪ allow_all_keys servers. A key with
    no MCP grant of its own is `(inherited: …)` only when a team, an
    organisation or its owner's admin role could grant servers (`inherits_from`
    names them; not resolved here). With none of those the gateway denies it
    outright, and it shows `(none)` (`reaches: []`).
    """
    c = _conn(ctx)
    servers = _call(all_mcp_servers, c)
    want = mcp_mod.server_label(_resolve_mcp(c, server_ref, servers)) if server_ref else None
    names = {s.get("server_id"): mcp_mod.server_label(s) for s in servers}
    open_to_all = sorted(mcp_mod.server_label(s) for s in servers if s.get("allow_all_keys"))
    keys = _call(all_keys, c, team)
    roles: dict[str, str | None] = {}
    if any(
        k.get("user_id") and not mcp_mod.key_mcp(k)["access_groups"] and not mcp_mod.key_mcp(k)["servers"]
        for k in keys
    ):
        try:
            roles = {u.get("user_id"): u.get("user_role") for u in all_users(c)}
        except be.ApiError:
            roles = {}  # unreadable roles: an owner's admin role cannot be confirmed
    rows = []
    for k in keys:
        g = mcp_mod.key_mcp(k)
        reaches = mcp_mod.effective_servers(g, servers)
        sources: list[str] = []
        if reaches is None:
            sources = mcp_mod.inherit_sources(k, roles)
            if not sources:
                reaches = open_to_all  # default-deny: only servers open to every key
        if want and (reaches is None or want not in reaches):
            continue
        rows.append(
            {
                "key_alias": k.get("key_alias") or k.get("key_name"),
                "team_id": k.get("team_id"),
                "access_groups": g["access_groups"],
                "servers": [names.get(s, s) for s in g["servers"]],
                "reaches": reaches,
                "inherits_from": sources,
            }
        )
    _emit(
        ctx,
        {"server": want, "keys": rows},
        lambda d: _table(
            [
                {
                    **_list_str(r, "access_groups", "servers"),
                    "reaches": mcp_mod.reach_label(r["reaches"], r["inherits_from"]),
                }
                for r in d["keys"]
            ],
            [
                ("key_alias", "KEY"),
                ("team_id", "TEAM"),
                ("access_groups", "GROUPS"),
                ("servers", "SERVERS"),
                ("reaches", "REACHES"),
            ],
        ),
    )


def _mcp_server_options(fn):
    for opt in reversed(
        [
            click.option("--description", default=None),
            click.option("--url", default=None, help="Server endpoint, e.g. https://mcp.example/mcp."),
            click.option("--transport", type=click.Choice(mcp_mod.TRANSPORTS), default=None),
            click.option("--auth-type", type=click.Choice(mcp_mod.AUTH_TYPES), default=None),
            click.option(
                "--auth-value-env",
                default=None,
                metavar="VAR",
                help="Env var holding the upstream credential (never passed on the command line).",
            ),
            click.option("--access-groups", default=None, help="Comma-separated MCP access groups."),
            click.option(
                "--allowed-tools", default=None, help="Comma-separated tool allowlist (unprefixed names)."
            ),
        ]
    ):
        fn = opt(fn)
    return fn


@mcp_grp.command("add")
@click.option("--name", required=True, help="server_name; clients see tools as <alias-or-name>-<tool>.")
@click.option("--alias", default=None, help="Tool prefix, if different from --name.")
@click.option("--server-id", default=None, help="Pin the id; the proxy generates one if omitted.")
@_mcp_server_options
@click.option("--allow-all-keys", is_flag=True, help="Every key may use this server, grant or not.")
@click.pass_context
def mcp_add(
    ctx,
    name,
    alias,
    server_id,
    description,
    url,
    transport,
    auth_type,
    auth_value_env,
    access_groups,
    allowed_tools,
    allow_all_keys,
):
    """Add an MCP server to the proxy's database (POST /v1/mcp/server).

    A DB-only server: it survives a config redeploy and is not in git.
    `--transport` defaults to http (the proxy's own default is sse). stdio
    servers are refused here: they run a process on the proxy host and belong
    in reviewed config.
    """
    try:
        secret = mcp_mod.secret_from_env(auth_value_env)
        body = mcp_mod.new_server(
            name,
            url=url,
            transport=transport or "http",
            server_id=server_id,
            alias=alias,
            description=description,
            auth_type=auth_type,
            auth_value=secret,
            access_groups=mcp_mod.split_csv(access_groups),
            allowed_tools=mcp_mod.split_csv(allowed_tools),
            allow_all_keys=allow_all_keys,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if _dry(ctx, "POST", "/v1/mcp/server", mcp_mod.masked_body(body, auth_value_env)):
        return
    res = _call(be.post, _conn(ctx), "/v1/mcp/server", body) or {}
    out = mcp_mod.redact_server(res) if isinstance(res, dict) else res
    _emit(
        ctx, out, lambda r: click.echo(f"  added MCP server {mcp_mod.server_label(r)} ({r.get('server_id')})")
    )


@mcp_grp.command("update")
@click.argument("server_ref")
@click.option("--alias", default=None)
@_mcp_server_options
@click.option(
    "--allow-all-keys/--no-allow-all-keys", default=None, help="Open to every key, or back to grants only."
)
@click.pass_context
def mcp_update(
    ctx,
    server_ref,
    alias,
    description,
    url,
    transport,
    auth_type,
    auth_value_env,
    access_groups,
    allowed_tools,
    allow_all_keys,
):
    """Change a DB server (PUT /v1/mcp/server): only the flags given are sent.

    `--access-groups ''` / `--allowed-tools ''` clear the list. Config servers
    are refused: the API cannot change them.
    """
    c = _conn(ctx)
    s = _resolve_mcp(c, server_ref)
    _mcp_db_only(s, "change")
    try:
        body = mcp_mod.update_server(
            s["server_id"],
            alias=alias,
            description=description,
            url=url,
            transport=transport,
            auth_type=auth_type,
            auth_value=mcp_mod.secret_from_env(auth_value_env),
            access_groups=mcp_mod.split_csv(access_groups),
            allowed_tools=mcp_mod.split_csv(allowed_tools),
            allow_all_keys=allow_all_keys,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if _dry(ctx, "PUT", "/v1/mcp/server", mcp_mod.masked_body(body, auth_value_env)):
        return
    res = _call(be.put, c, "/v1/mcp/server", body) or {}
    _emit(ctx, mcp_mod.redact_server(res) if isinstance(res, dict) else res)


@mcp_grp.command("delete")
@click.argument("server_ref")
@click.option("--yes", is_flag=True)
@click.pass_context
def mcp_delete(ctx, server_ref, yes):
    """Remove a DB server (DELETE /v1/mcp/server/{id}); its tools vanish from every client.

    The proxy does not clean up grants: keys that name the server keep the
    stale id (ignored at runtime). The output names them so they can be
    re-granted with `keys update --mcp-servers`.
    """
    c = _conn(ctx)
    s = _resolve_mcp(c, server_ref)
    _mcp_db_only(s, "remove")
    path = mcp_mod.server_path(s["server_id"])
    if _dry(ctx, "DELETE", path):
        return
    stale = [k.get("key_alias") or k.get("token") for k in mcp_mod.keys_granting(_call(all_keys, c), s)]
    _confirm(ctx, yes, f"delete MCP server {mcp_mod.server_label(s)} (named by {len(stale)} key grant(s))")
    _call(be.delete, c, path)
    _emit(
        ctx,
        {"deleted": s["server_id"], "name": mcp_mod.server_label(s), "keys_with_stale_grant": stale},
        lambda d: (
            click.echo(f"deleted MCP server {d['name']} ({d['deleted']})"),
            click.echo("keys still naming it: " + (", ".join(d["keys_with_stale_grant"]) or "(none)")),
        ),
    )


SESSION_COLS = [
    ("session_id_prefix", "SESSION"),
    ("client_name", "CLIENT"),
    ("user_id", "USER"),
    ("key_alias", "KEY"),
    ("client_ip", "IP"),
    ("idle_seconds", "IDLE(s)"),
    ("in_flight_requests", "IN-FLIGHT"),
]


@mcp_grp.command("sessions")
@click.pass_context
def mcp_sessions(ctx):
    """Live stateful gateway sessions — on the ONE worker that answered.

    The proxy reports per process (`worker_pid`); with several workers or
    pods, repeat the call or target each node with --url.
    """
    res = _call(be.get, _conn(ctx), "/v1/mcp/sessions") or {}
    _emit(
        ctx,
        res,
        lambda r: (
            click.echo(f"  worker {r.get('worker_pid')}: {r.get('total_sessions', 0)} session(s)"),
            _table(r.get("sessions") or [], SESSION_COLS),
        ),
    )


@mcp_grp.command("sessions-close")
@click.option(
    "--prefix", default=None, help=f"Session id prefix (at least {mcp_mod.SESSION_PREFIX_MIN} chars)."
)
@click.option("--user", default=None, help="Close every session this LiteLLM user_id opened.")
@click.option("--yes", is_flag=True)
@click.pass_context
def mcp_sessions_close(ctx, prefix, user, yes):
    """Force-close live gateway sessions (DELETE /v1/mcp/sessions), on the worker that answers."""
    if not prefix and not user:
        raise click.ClickException("select sessions with --prefix and/or --user")
    if prefix and len(prefix) < mcp_mod.SESSION_PREFIX_MIN:
        raise click.ClickException(f"--prefix needs at least {mcp_mod.SESSION_PREFIX_MIN} characters")
    params = {k: v for k, v in {"session_id_prefix": prefix, "user_id": user}.items() if v}
    if ctx.obj["dry_run"]:
        _emit(ctx, {"dry_run": True, "would": "DELETE /v1/mcp/sessions", "params": params})
        return
    _confirm(ctx, yes, "close MCP sessions " + ", ".join(f"{k}={v}" for k, v in params.items()))
    res = _call(be.delete, _conn(ctx), "/v1/mcp/sessions", params=params) or {}
    _emit(
        ctx,
        res,
        lambda r: (
            click.echo(
                f"  worker {r.get('worker_pid')}: closed {r.get('terminated_sessions', 0)} session(s)"
            ),
            _table(r.get("sessions") or [], SESSION_COLS),
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
    """Git config vs the live proxy: deployments, DB-only models, router (fallback chains too), guardrails."""
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
