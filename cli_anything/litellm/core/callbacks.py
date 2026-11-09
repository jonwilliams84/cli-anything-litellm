"""Logging-callback management: which integrations actually log a request.

A *logging callback* is the proxy's catch-all for observability integrations —
Langfuse, OpenTelemetry, S3, Datadog, Langsmith, Lago, a custom HTTP endpoint…
Each one is a logger the proxy calls with the success and/or failure event of
every request, configured from config.yaml's ``litellm_settings`` (e.g.
``success_callback: [langfuse]``) or, for the ones added at runtime, from the
proxy's own DB config: ``litellm_settings.success_callback`` plus the callback's
environment variables (``environment_variables`` section, values encrypted at
rest by the proxy).

The runtime view and the stored view answer different questions:

- ``GET /callbacks/list`` (``callback_management_endpoints.list_callbacks``) —
  what is *active on this worker right now*, from
  ``logging_callback_manager.get_callbacks_by_type()``: three lists
  (``success``, ``failure``, ``success_and_failure``). It includes internal
  loggers (spend tracking, the alerting instance…) that nobody configured and
  that cannot be deleted, and hides the class instances that stand in for a
  configured name.
- ``GET /get/config/callbacks`` (``proxy_server.get_config``) — what the proxy
  has *stored and where it gets it from*: one row per configured callback
  (``litellm_settings.success_callback`` / ``failure_callback`` / ``callbacks``,
  via ``callback_utils.process_callback``) with its environment variables from
  the stored DB section, or the process env — plus the runtime-only loggers as
  rows flagged ``read_only`` (``_hidden_runtime_callback_names`` filters the
  internal ones), the alerting destinations (slack/email/ms_teams), the router
  settings the router is running on, and the catalogue of callbacks the Admin
  UI can add (``AllCallbacks``: name → the env vars each integration wants).

Mutations (``POST /config/update`` — ``update_config``; ``POST
/config/callback/delete`` — ``delete_callback``) write only the DB store, need
an admin role and a connected DB, and refuse a key config.yaml owns (HTTP 400
with a ``resolution`` in the error detail): the DB row would never win over the
file at the next restart. ``update_config`` is partial: sections absent from
the body are untouched. ``success_callback`` is special-cased *additive* — the
proxy unions the request over the stored list and dedupes — while any other
``litellm_settings`` (e.g. ``failure_callback``) would be replaced by the
request's value; the CLI only ever sends ``success_callback`` for that reason.
``delete_callback`` only removes from ``litellm_settings.success_callback`` (404
``not found in active configuration`` otherwise) — a failure-only logger is not
reachable through it.

Shape notes (tolerated variants):

- ``CallbacksByType`` values may be scalars on older proxies (a single callback
  name instead of a list).
- ``/get/config/callbacks`` rows: string callbacks normalise to lists upstream,
  but a row's ``variables`` may miss keys the callback expects (the value is
  neither stored nor in the process env) and a non-admin caller gets
  ``REDACTED`` instead of values (the proxy's own gate).
- Stored ``success_callback`` entries may be mixed case from another writer;
  ``delete_callback`` looks its name up **lower-cased**, so the CLI sends
  ``callback_name`` lower-cased too.
"""

from __future__ import annotations

import os
import re
from typing import Any

BY_TYPES = ("success", "failure", "success_and_failure")

MASK = "****"

#: Variable names whose value is a credential — mask in every echo. Matches the
#: keys ``is_sensitive_callback_key`` encrypts (and the ones
#: ``_EXTRA_SECRET_CALLBACK_ENV_VARS`` adds: a webhook URL or an HTTP header is
#: a credential too), erring on the side of masking.
SENSITIVE = re.compile(
    r"authorization|token|key|secret|credential|password|passwd|auth|header|webhook|url",
    re.I,
)

#: Alias → canonical name (``proxy_server._normalize_callback_alias``): a
#: callback may be configured under either spelling, so resolution treats them
#: as the same callback.
ALIASES = (
    ("opentelemetry", "otel"),
    ("s3_v2", "s3"),
    ("aws_sqs", "sqs"),
    ("custom_callback_api", "generic_api"),
)


def canonical_name(name: str) -> str:
    """The name under which a callback is indexed, alias folded away.

    ``/callbacks/list`` already reports canonical names; the stored view may
    hold the alias config.yaml spelled, so comparing by the canonical form
    does not make one callback look like two.
    """
    return next((c for alias, c in ALIASES if alias == name), name)


def _sensitive(key: str) -> bool:
    return bool(SENSITIVE.search(key or ""))


def as_by_type(res: Any) -> dict[str, list[str]]:
    """Whatever ``/callbacks/list`` returned, as ``{success: [...], …}``.

    Values are lists of names; a bare string (an older shape) becomes a single
    item list, names are canonicalised, missing lists come back empty.
    """
    res = res or {}
    out: dict[str, list[str]] = {}
    for t in BY_TYPES:
        v = (res.get(t) or res.get(f"{t}s") or []) if isinstance(res, dict) else []
        items = v if isinstance(v, list) else [v]
        out[t] = [canonical_name(c) for c in items if isinstance(c, str) and c]
    return out


def as_rows(res: Any) -> list[dict[str, Any]]:
    """Whatever ``/get/config/callbacks`` returned, as its callback rows.

    The list lives under the ``callbacks`` key (``get_config``'s own list of
    stored + runtime rows); a bare list and ``{"data": [...]}`` are accepted
    as well, ``None``/junk → no rows.
    """
    items: Any = res
    if isinstance(res, dict):
        items = None
        for key in ("callbacks", "data"):
            if isinstance(res.get(key), list):
                items = res[key]
                break
    if not isinstance(items, list):
        return []
    return [r for r in items if isinstance(r, dict) and r.get("name")]


def normalize(row: dict[str, Any]) -> dict[str, Any]:
    """One row per callback, as `callbacks config` shows it.

    ``type`` defaults to ``success`` (how ``get_config`` spells a row it adds);
    ``read_only`` marks a runtime-only logger. Variables are redacted here:
    stored secrets (the proxy encrypts them at rest, but an *admin* caller
    reads them back decrypted) are masked before they can be echoed.
    """
    return {
        "name": row.get("name"),
        "type": row.get("type") or "success",
        "read_only": bool(row.get("read_only")),
        "variables": redact_variables(row.get("variables") or {}),
    }


def redact_variables(variables: Any) -> dict[str, str | None]:
    """Callback variables safe to print: credential-named values masked.

    The proxy already redacts for a non-admin caller; a master-key caller gets
    real values and this catches them. ``REDACTED`` (the proxy's own mask)
    passes through unchanged, ``None`` (a variable the callback declares but
    neither store nor env has) too.
    """
    if not isinstance(variables, dict):
        return {}
    out: dict[str, str | None] = {}
    for k, v in variables.items():
        if v in (None, MASK, "REDACTED"):
            out[k] = v
        elif _sensitive(k):
            out[k] = MASK
        else:
            out[k] = v
    return out


def resolve(rows: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """Match ``ref`` — exact name, case-insensitive, alias folded — against rows.

    Returns ``{by, matches, row}`` or None — the shape every resolver in this
    CLI returns, so the calling command can refuse an unknown reference it
    would otherwise let the proxy 404.
    """
    if not ref:
        return None
    want = canonical_name(ref.lower())
    for row in rows:
        if canonical_name(str(row.get("name", "")).lower()) == want:
            return {"by": "name", "matches": [row.get("name")], "row": row}
    return None


def parse_envs(pairs: tuple[str, ...] | list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    """``--env KEY=VALUE`` pairs → ``environment_variables`` for the add body.

    Values are env-var values: they stay strings (no number-true-false typing).
    A secret-named value must be written ``os.environ/NAME`` — resolved from
    the CLI's own environment before the call, so the secret stays out of the
    shell history and of this CLI's output; anything sensitive sent literally
    is refused.
    """
    out: dict[str, Any] = {}
    for p in pairs:
        if "=" not in p:
            raise ValueError(f"--env takes KEY=VALUE (got {p!r})")
        k, v = p.split("=", 1)
        k = k.strip()
        if not k:
            raise ValueError(f"--env takes KEY=VALUE (got {p!r})")
        if _sensitive(k) and not v.startswith("os.environ/"):
            raise ValueError(f"--env {k}: pass secrets as os.environ/NAME, never literally")
        if v.startswith("os.environ/"):
            var = v.split("/", 1)[1]
            val = (env if env is not None else os.environ).get(var)
            if not val:
                raise ValueError(f"--env {k}: environment variable {var} is unset or empty")
            v = val
        out[k] = v
    return out


def add_body(name: str, rows: list[dict[str, Any]], envs: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ``POST /config/update`` body that logs ``name`` on success events.

    Only ``litellm_settings.success_callback`` is sent: the proxy unions the
    request over the stored list (``update_config`` dedupes and lower-cases),
    so sending the new name alone adds it without touching the others — and is
    exactly what the Admin UI's add forms do. ``environment_variables`` rides
    in the same call when ``--env`` values were given (the proxy encrypts
    sensitive ones at rest and merges per key, idempotently).

    Refuses (before the proxy 400s):

    - an empty name — the callback is what the body is about;
    - a name already configured (any type) — the call would be a no-op at
      best, and the stored list is not sent, so nothing would change;
    - a read-only runtime logger (it comes from the proxy's own wiring or a
      config.yaml section, so the DB row would never apply).
    """
    if not name or not name.strip():
        raise ValueError("name the callback — run `callbacks config`")
    hit = resolve(rows, name.strip())
    if hit:
        kind = (
            "a runtime callback not configurable here"
            if hit["row"].get("read_only")
            else "already configured"
        )
        raise ValueError(f"callback {name!r} is {kind} — run `callbacks config`")
    body: dict[str, Any] = {"litellm_settings": {"success_callback": [name.lower()]}}
    if envs is not None and envs:
        body["environment_variables"] = envs
    return body


def masked_body(body: dict[str, Any], envs: dict[str, Any] | None = None) -> dict[str, Any]:
    """The add body as `--dry-run` prints it: resolved secret values never appear.

    ``environment_variables`` is the one section carrying credentials; its
    sensitive-named values are swapped for a marker naming the variable the
    value came from, so the dry run is checkable without being leakable.
    """
    if not envs:
        return body
    shown = dict(body)
    shown["environment_variables"] = {
        k: (MASK if _sensitive(k) else body["environment_variables"][k])
        for k in body["environment_variables"]
    }
    return shown


def delete_body(rows: list[dict[str, Any]], ref: str) -> dict[str, Any]:
    """The ``POST /config/callback/delete`` body plus the refusals that matter.

    The endpoint only removes from ``litellm_settings.success_callback`` (it
    looks the name up lower-cased): a ``failure`` or ``success_and_failure``
    row is not reachable through it, a read-only runtime logger is not
    reachable at all — both are refused client-side with the reason, instead of
    the proxy's 404 ``not found in active configuration``, which is silent
    about *why*.
    """
    hit = resolve(rows, ref)
    if not hit:
        raise ValueError(f"no stored callback matches {ref!r} — run `callbacks config`")
    row = hit["row"]
    if row.get("read_only"):
        raise ValueError(f"callback {ref!r} is a runtime callback, not a stored one — it cannot be deleted")
    if (row.get("type") or "success") != "success":
        raise ValueError(
            f"callback {ref!r} is a {row.get('type')} callback; /config/callback/delete only removes"
            " success callbacks — edit config.yaml for the others"
        )
    return {"callback_name": str(row.get("name")).lower()}


def available(res: Any) -> dict[str, list[str]]:
    """From the stored view's ``available_callbacks`` (``AllCallbacks``), what can be
    added and which environment variables each callback expects — the checklist
    `callbacks add` hands over for a name, so "which vars does langfuse want?"
    is answerable from the CLI that adds it."""
    box = res.get("available_callbacks") if isinstance(res, dict) else None
    if not isinstance(box, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, v in box.items():
        if isinstance(v, dict):
            out[v.get("litellm_callback_name") or key] = list(v.get("litellm_callback_params") or [])
    return out


def alerts(res: Any) -> list[dict[str, Any]]:
    """The stored view's ``alerts`` — the alerting destinations, redacted like the
    callback rows (a slack webhook URL is a credential)."""
    box = res.get("alerts") if isinstance(res, dict) else None
    if not isinstance(box, list):
        return []
    out = []
    for a in box:
        if isinstance(a, dict):
            out.append({**a, "variables": redact_variables(a.get("variables"))})
    return out
