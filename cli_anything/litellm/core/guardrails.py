"""LiteLLM guardrail management (``/guardrails*``, ``/v2/guardrails/list``).

Guardrails, like models and MCP servers, come from two places: ``guardrails:``
in config.yaml (``guardrail_definition_location: "config"``, not editable
through the API) and rows in the proxy's database. The endpoints, as v1.106.3
has them (``litellm/proxy/guardrails/guardrail_endpoints.py`` and
``usage_endpoints.py``):

- ``GET /guardrails/list`` (``list_guardrails``): config.yaml ONLY — what
  ``drift`` compares. ``GET /v2/guardrails/list`` (``list_guardrails_v2``):
  active DB rows plus config guardrails, each with
  ``guardrail_definition_location``.
- ``GET /guardrails/{id}/info`` (``get_guardrail_info``): one, masked.
- ``POST /guardrails`` (``create_guardrail``): body ``{"guardrail": Guardrail}``.
- ``PATCH /guardrails/{id}`` (``patch_guardrail``): ``PatchGuardrailRequest`` —
  ONLY ``guardrail_name``, ``litellm_params`` (``BaseLitellmParams``: no
  ``guardrail`` or ``mode`` field, so neither can be patched; ``default_on``
  lives inside ``litellm_params``, despite the endpoint's own docstring
  showing it top-level, where it is silently ignored) and ``guardrail_info``
  (which REPLACES the stored dict). ``PUT`` replaces the whole guardrail, and
  since every read is masked, a read-modify-PUT would write the masked
  ``api_key`` back — this module never PUTs.
- ``DELETE /guardrails/{id}`` (``delete_guardrail``): DB rows only.
- ``POST /guardrails/apply_guardrail`` (``apply_guardrail``): runs one guardrail
  on a text; ``{"response_text"}`` or an HTTP error when it blocks.
- ``GET /guardrails/usage/overview|detail/{id}|logs`` (``guardrails_usage_*``):
  the daily metrics tables, window ≤ 366 days, logs paged ≤ 100.
- ``GET /guardrails/submissions`` + ``POST …/{id}/approve|reject``: the team
  registration queue (approve/reject only from ``pending_review``).

Masking: the proxy's ``get_masked_values`` keeps 2+2 characters of anything
under a key containing token/key/secret/credentials/password; an ``api_base``
can still carry a key in its path or query. :func:`redact` masks those values
fully and cuts URLs to their origin.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from cli_anything.litellm.core.mcp import as_rows, url_origin

#: ``GuardrailEventHooks`` (``litellm/types/guardrails.py``).
MODES = (
    "pre_call",
    "post_call",
    "during_call",
    "logging_only",
    "pre_mcp_call",
    "during_mcp_call",
    "post_mcp_call",
    "realtime_input_transcription",
)

#: ``_USAGE_MAX_RANGE_DAYS`` (``proxy/guardrails/usage_endpoints.py``).
MAX_RANGE_DAYS = 366

#: ``guardrails_usage_logs`` ``page_size`` bound (``le=100``).
LOGS_PAGE_MAX = 100

#: The keywords ``get_masked_values`` (``litellm_core_utils/litellm_logging.py``)
#: treats as sensitive, plus ``auth``/``header`` for custom header maps.
SENSITIVE = re.compile(r"authorization|token|key|secret|credential|password|passwd|auth|header", re.I)
URL_FIELDS = ("api_base", "api_endpoint")
MASK = "****"

#: ``usage_endpoints._status_from_fail_rate``: >15% critical, >5% warning.
USAGE_STATUSES = ("healthy", "warning", "critical")

ACTIONS = ("blocked", "passed", "flagged", "not_run")


def location(g: dict[str, Any]) -> str:
    """``config`` or ``db`` (``GuardrailInfoResponse`` defaults to config)."""
    return str(g.get("guardrail_definition_location") or "config")


def _redact_value(k: str, v: Any) -> Any:
    if isinstance(v, dict):
        return {kk: _redact_value(str(kk), vv) for kk, vv in v.items()}
    if isinstance(v, list):
        return [_redact_value(k, x) for x in v]
    if k in URL_FIELDS and isinstance(v, str):
        return v if v.startswith("os.environ/") else url_origin(v)
    if isinstance(v, str) and SENSITIVE.search(k) and v:
        return v if v.startswith("os.environ/") else MASK
    return v


def redact(g: dict[str, Any]) -> dict[str, Any]:
    """A copy safe to print: secret-named values fully masked, URLs cut to their origin.

    ``os.environ/NAME`` references are kept: they name a variable, not a secret.
    """
    out = dict(g)
    if isinstance(g.get("litellm_params"), dict):
        out["litellm_params"] = {k: _redact_value(k, v) for k, v in g["litellm_params"].items()}
    return out


def normalize(g: dict[str, Any]) -> dict[str, Any]:
    """One row per guardrail (table and --json of `guardrails list --all`)."""
    p = g.get("litellm_params") or {}
    mode = p.get("mode")
    info = g.get("guardrail_info") or {}
    return {
        "guardrail_id": g.get("guardrail_id"),
        "name": g.get("guardrail_name"),
        "source": location(g),
        "guardrail": p.get("guardrail"),
        "mode": ",".join(mode) if isinstance(mode, list) else (mode if isinstance(mode, str) else None),
        "default_on": p.get("default_on"),
        "api_base": url_origin(p.get("api_base")) if p.get("api_base") else None,
        "description": info.get("description") if isinstance(info, dict) else None,
    }


def resolve(guardrails: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """Exact guardrail_id, then a UNIQUE guardrail_name. Raises ValueError on an ambiguous name."""
    if not ref:
        return None
    for g in guardrails:
        if g.get("guardrail_id") == ref:
            return g
    hits = [g for g in guardrails if g.get("guardrail_name") == ref]
    if len(hits) > 1:
        raise ValueError(
            f"{ref!r} names {len(hits)} guardrails — use the guardrail_id (`guardrails list --all`)"
        )
    return hits[0] if hits else None


def parse_modes(raw: str | None) -> str | list[str] | None:
    """``pre_call`` or ``pre_call,post_call`` → the proxy's ``mode`` (str or list)."""
    if raw is None:
        return None
    modes = [m.strip() for m in raw.split(",") if m.strip()]
    bad = [m for m in modes if m not in MODES]
    if not modes or bad:
        raise ValueError(f"--mode must be one or more of {', '.join(MODES)} (got {raw!r})")
    return modes[0] if len(modes) == 1 else modes


def parse_params(pairs: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """``--param k=v`` pairs. ``true``/``false``/numbers are typed; secrets are refused.

    A literal credential would be stored in the proxy's DB and echoed by any
    shell history; use ``--api-key os.environ/NAME`` instead.
    """
    out: dict[str, Any] = {}
    for p in pairs:
        if "=" not in p:
            raise ValueError(f"--param takes key=value (got {p!r})")
        k, v = p.split("=", 1)
        k = k.strip()
        if not k:
            raise ValueError(f"--param takes key=value (got {p!r})")
        if SENSITIVE.search(k) and not v.startswith("os.environ/"):
            raise ValueError(f"--param {k}: pass secrets as os.environ/NAME, never literally")
        low = v.lower()
        if low in ("true", "false"):
            out[k] = low == "true"
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
    return out


def check_api_key(api_key: str | None) -> None:
    """Only ``os.environ/NAME`` references: the proxy resolves them at load time
    (``InMemoryGuardrailHandler.initialize_guardrail``) and the secret never
    enters the DB, argv history or this CLI's output."""
    if api_key and not api_key.startswith("os.environ/"):
        raise ValueError(
            "--api-key takes os.environ/NAME (the proxy reads the variable); never a literal key"
        )


def new_guardrail(
    name: str,
    guardrail: str,
    mode: str | None,
    *,
    default_on: bool = False,
    api_base: str | None = None,
    api_key: str | None = None,
    description: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``POST /guardrails`` body (``CreateGuardrailRequest``). Raises ValueError."""
    if not name:
        raise ValueError("--name is required")
    if not guardrail:
        raise ValueError(
            "--guardrail (the integration, e.g. presidio, lakera_v2, generic_guardrail_api) is required"
        )
    modes = parse_modes(mode)
    if modes is None:
        raise ValueError("--mode is required (e.g. pre_call)")
    check_api_key(api_key)
    lp: dict[str, Any] = {
        **(params or {}),
        "guardrail": guardrail,
        "mode": modes,
        "default_on": bool(default_on),
    }
    if api_base:
        lp["api_base"] = api_base
    if api_key:
        lp["api_key"] = api_key
    body: dict[str, Any] = {"guardrail_name": name, "litellm_params": lp}
    if description is not None:
        body["guardrail_info"] = {"description": description}
    return {"guardrail": body}


def patch_body(
    current: dict[str, Any],
    *,
    name: str | None = None,
    default_on: bool | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    description: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``PATCH /guardrails/{id}`` body (``PatchGuardrailRequest``). Raises ValueError.

    ``default_on`` goes INSIDE ``litellm_params`` (the only place the request
    model reads it). ``guardrail_info`` replaces the stored dict, so a new
    description is merged over the current info. ``mode`` and ``guardrail``
    are not patchable (``BaseLitellmParams`` has neither field).
    """
    check_api_key(api_key)
    params = dict(params or {})
    for fixed in ("mode", "guardrail"):
        if fixed in params:
            raise ValueError(
                f"{fixed} cannot be changed in place (PATCH ignores it; PUT would write the masked api_key back) "
                "— delete and re-add the guardrail"
            )
    lp: dict[str, Any] = dict(params)
    if default_on is not None:
        lp["default_on"] = default_on
    if api_base is not None:
        lp["api_base"] = api_base
    if api_key is not None:
        lp["api_key"] = api_key
    body: dict[str, Any] = {}
    if name is not None:
        body["guardrail_name"] = name
    if lp:
        body["litellm_params"] = lp
    if description is not None:
        info = current.get("guardrail_info") if isinstance(current.get("guardrail_info"), dict) else {}
        body["guardrail_info"] = {**info, "description": description}
    if not body:
        raise ValueError("nothing to update")
    return body


def verdict(text: str, result: Any) -> dict[str, Any]:
    """What `apply_guardrail` did to the text: passed (unchanged) or modified (e.g. masked)."""
    out = (result or {}).get("response_text") if isinstance(result, dict) else None
    if out is None:
        return {"verdict": "unknown", "response_text": None}
    return {"verdict": "passed" if out == text else "modified", "response_text": out}


def window(days: int | None, start: str | None, end: str | None, today: date | None = None) -> dict[str, str]:
    """``start_date``/``end_date`` (YYYY-MM-DD) for the usage endpoints. Raises ValueError.

    The proxy's own default is the last 7 days; its limit is 366 days.
    """
    if (start is None) != (end is None):
        raise ValueError("--start and --end go together")
    if start is not None and end is not None:
        try:
            s, e = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError as exc:
            raise ValueError("--start/--end must be YYYY-MM-DD") from exc
    else:
        if days is None or days < 1:
            raise ValueError("--days must be at least 1")
        e = today or date.today()
        s = e - timedelta(days=days)
    if e < s:
        raise ValueError("--start must be on or before --end")
    if (e - s).days > MAX_RANGE_DAYS:
        raise ValueError(f"the proxy refuses windows over {MAX_RANGE_DAYS} days")
    return {"start_date": s.isoformat(), "end_date": e.isoformat()}


def usage_rows(res: Any) -> list[dict[str, Any]]:
    """``UsageOverviewResponse.rows`` as snake_case rows, busiest first."""
    rows = []
    for r in as_rows(res, "rows"):
        rows.append(
            {
                "guardrail_id": r.get("id"),
                "name": r.get("name"),
                "provider": r.get("provider"),
                "requests": int(r.get("requestsEvaluated") or 0),
                "fail_rate": r.get("failRate"),
                "status": r.get("status"),
                "trend": r.get("trend"),
                "cost": r.get("cost"),
            }
        )
    rows.sort(key=lambda x: (-x["requests"], x["name"] or ""))
    return rows


def usage_totals(res: Any) -> dict[str, Any]:
    r = res if isinstance(res, dict) else {}
    return {
        "requests": int(r.get("totalRequests") or 0),
        "blocked": int(r.get("totalBlocked") or 0),
        "pass_rate": r.get("passRate"),
        "cost": r.get("totalCost"),
    }


def log_rows(res: Any, snippets: bool = False) -> list[dict[str, Any]]:
    """``UsageLogsResponse.logs``. Input/output snippets are prompt and response
    text (they can hold anything a user typed): left out unless asked for."""
    out = []
    for e in as_rows(res, "logs"):
        row = {
            "request_id": e.get("id"),
            "timestamp": e.get("timestamp"),
            "action": e.get("action"),
            "score": e.get("score"),
            "latency_ms": e.get("latency_ms"),
            "model": e.get("model"),
            "reason": (e.get("reason") or "")[:160] or None,
        }
        if snippets:
            row["input_snippet"] = e.get("input_snippet")
            row["output_snippet"] = e.get("output_snippet")
        out.append(row)
    return out


def submission_rows(res: Any) -> list[dict[str, Any]]:
    """``ListGuardrailSubmissionsResponse.submissions``, redacted, one row each."""
    out = []
    for s in as_rows(res, "submissions"):
        p = s.get("litellm_params") or {}
        out.append(
            {
                "guardrail_id": s.get("guardrail_id"),
                "name": s.get("guardrail_name"),
                "status": s.get("status"),
                "team_id": s.get("team_id"),
                "guardrail": p.get("guardrail"),
                "mode": p.get("mode"),
                "api_base": url_origin(p.get("api_base")) if p.get("api_base") else None,
                "submitted_by": s.get("submitted_by_email") or s.get("submitted_by_user_id"),
                "submitted_at": s.get("submitted_at"),
            }
        )
    return out
