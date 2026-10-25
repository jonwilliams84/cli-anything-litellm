"""Router fallbacks: which model group takes over when another one fails.

A fallback rule is ``{model_group: [fallback, ...]}``, tried in order after the
group's own retries are spent. There are three lists, one per failure class,
kept in ``router_settings`` (``litellm/proxy/management_endpoints/
fallback_management_endpoints.py`` and ``router_utils/fallback_event_handlers.py``
in v1.106.3):

- ``fallbacks`` (type ``general``): any error.
- ``context_window_fallbacks`` (``context_window``): context window exceeded.
- ``content_policy_fallbacks`` (``content_policy``): content policy violation.

The endpoints:

- ``GET /router/settings`` (``get_router_settings``): ``current_values`` holds
  all three lists; ``source`` names who owns each field: ``config``
  (config.yaml), ``db``, ``env``, ``default`` or ``unset``.
- ``GET /fallback/{model}?fallback_type=`` (``get_fallback``): the chain the
  router would actually use for ``model`` (``get_all_fallbacks`` →
  ``get_fallback_model_group``: exact key, then a provider-prefixed or
  stripped match, then the ``"*"`` catch-all). 404 when there is none.
- ``POST /fallback`` (``create_fallback``): body ``FallbackCreateRequest``
  ``{"model", "fallback_models", "fallback_type"}``. Replaces the model's rule
  of that type (or appends one), writes ``router_settings`` to the DB and sets
  it on the answering worker's router. Needs ``STORE_MODEL_IN_DB``.
- ``DELETE /fallback/{model}?fallback_type=`` (``delete_fallback``): drops the
  rule; 404 when none matches.

The trap: when config.yaml sets a fallback list, the config file OWNS it
(``config_ownership_message`` in ``config_resolvers/settings_store.py``: "The
value stored in the database for it is ignored and will never be applied").
``POST /fallback`` still changes the live router on the worker that answered,
but ``/router/settings`` keeps showing the config value, the other workers
never see the change, and the next restart drops it. :func:`config_owned`
lets the CLI refuse those writes and point at the config file instead.

Testing a chain: a completion carrying ``mock_testing_fallbacks: true`` (or the
context-window / content-policy variants, ``MockRouterTestingParams`` in
``types/router.py``) makes the router fail the primary on purpose
(``Router._handle_mock_testing_fallbacks``) so the request walks the chain.
The response's ``x-litellm-model-id`` header names the deployment that served.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

#: fallback_type → the router_settings field that holds that list.
FIELDS = {
    "general": "fallbacks",
    "context_window": "context_window_fallbacks",
    "content_policy": "content_policy_fallbacks",
}
TYPES = tuple(FIELDS)

#: fallback_type → the request flag that makes the router fail the primary.
MOCK_FLAGS = {
    "general": "mock_testing_fallbacks",
    "context_window": "mock_testing_context_fallbacks",
    "content_policy": "mock_testing_content_policy_fallbacks",
}

#: ``get_fallback_model_group`` treats a bare string entry as a chain of one
#: for EVERY model group; it is shown under this pseudo-key.
BARE = "(every model)"
CATCH_ALL = "*"


def fallback_path(model: str) -> str:
    """``/fallback/{model}`` with the name escaped (group names may hold ``/``)."""
    return f"/fallback/{quote(str(model), safe='')}"


def rules(value: Any) -> dict[str, list[str]]:
    """``{model: [fallbacks]}`` from a stored list, whatever shape it came in.

    The router keeps ``[{model: [..]}, ...]``; the DB row may hold that list as
    JSON text, and ``StoredFallback`` also admits bare strings. Later rules for
    the same key win, as ``POST /fallback`` replaces in place.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    out: dict[str, list[str]] = {}
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            for k, v in item.items():
                chain = v if isinstance(v, list) else [v]
                out[str(k)] = [str(x) for x in chain if isinstance(x, str)]
        elif isinstance(item, str):
            out.setdefault(BARE, []).append(item)
    return out


def config_owned(source: dict[str, Any] | None, fallback_type: str) -> bool:
    """True when config.yaml owns the list ``fallback_type`` writes to."""
    return (source or {}).get(FIELDS[fallback_type]) == "config"


def list_rows(
    current: dict[str, Any] | None,
    source: dict[str, Any] | None = None,
    *,
    model: str | None = None,
    fallback_type: str | None = None,
) -> list[dict[str, Any]]:
    """One row per rule across the three lists (``fallbacks list``).

    ``model`` keeps that group's own rule plus the ``*`` and bare catch-alls
    that would also apply to it.
    """
    current = current or {}
    out = []
    for t, field in FIELDS.items():
        if fallback_type and t != fallback_type:
            continue
        for m, chain in rules(current.get(field)).items():
            if model and m not in (model, CATCH_ALL, BARE):
                continue
            out.append(
                {
                    "model": m,
                    "type": t,
                    "fallbacks": chain,
                    "source": (source or {}).get(field),
                }
            )
    return out


def new_body(model: str, to: list[str], fallback_type: str, known: set[str] | None = None) -> dict[str, Any]:
    """Body for ``POST /fallback``. Raises ``ValueError`` on what the proxy would refuse.

    The same checks as ``FallbackCreateRequest`` and ``create_fallback``: a
    non-empty chain without duplicates, not the model itself, every name a live
    model group (``known``, when given).
    """
    model = (model or "").strip()
    if not model:
        raise ValueError("a model group is required")
    if fallback_type not in FIELDS:
        raise ValueError(f"--type must be one of {', '.join(TYPES)}")
    chain = [m.strip() for m in to if m and m.strip()]
    if not chain:
        raise ValueError("--to needs at least one fallback model group")
    dupes = sorted({m for m in chain if chain.count(m) > 1})
    if dupes:
        raise ValueError(f"--to lists {', '.join(dupes)} more than once")
    if model in chain:
        raise ValueError(f"{model!r} cannot be its own fallback")
    if known is not None:
        unknown = [m for m in [model, *chain] if m not in known and m != CATCH_ALL]
        if unknown:
            raise ValueError(
                f"not a live model group: {', '.join(unknown)} (see `models list`; the proxy refuses these too)"
            )
    return {"model": model, "fallback_models": chain, "fallback_type": fallback_type}


def chain_drift(cfg_router: dict[str, Any], live_values: dict[str, Any]) -> list[dict[str, Any]]:
    """Fallback rules that differ between a config's router_settings and a live proxy.

    Compared rule by rule (the order of rules does not matter, the order inside
    a chain does). A list the config does not set counts as empty, so rules
    added through ``POST /fallback`` (DB-only, never in git) are reported. A
    field the live side does not report at all (an older proxy) is skipped.
    """
    out = []
    for field in FIELDS.values():
        if field not in (live_values or {}):
            continue
        cfg_v = (cfg_router or {}).get(field)
        if isinstance(cfg_v, str) and cfg_v.startswith("os.environ/"):
            continue
        c, lv = rules(cfg_v), rules(live_values.get(field))
        for m in sorted(set(c) | set(lv)):
            if c.get(m) != lv.get(m):
                out.append({"key": f"{field}.{m}", "config": c.get(m), "live": lv.get(m)})
    return out


def probe_body(model: str, fallback_type: str, text: str, max_tokens: int = 16) -> dict[str, Any]:
    """The completion ``fallbacks test`` sends: the primary is failed on purpose."""
    if fallback_type not in MOCK_FLAGS:
        raise ValueError(f"--type must be one of {', '.join(TYPES)}")
    return {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": max_tokens,
        MOCK_FLAGS[fallback_type]: True,
    }


def probe_verdict(
    model: str,
    chain: list[str],
    headers: dict[str, Any],
    deployments: list[dict[str, Any]],
) -> dict[str, Any]:
    """What a mock-failed completion proves about ``model``'s chain.

    ``served_by`` is the model group of the deployment in ``x-litellm-model-id``.
    ``failed_over`` when a group in the chain served; ``not_triggered`` when the
    primary itself answered (the mock flag was ignored); ``unknown`` when the
    proxy sent no deployment id to judge by.
    """
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    dep_id = h.get("x-litellm-model-id")
    by_id = {(d.get("model_info") or {}).get("id"): d.get("model_name") for d in deployments or []}
    served = by_id.get(dep_id) if dep_id else None
    attempted = h.get("x-litellm-attempted-fallbacks")
    try:
        attempted = int(attempted) if attempted is not None else None
    except (TypeError, ValueError):
        attempted = None
    if served is None:
        verdict = "unknown"
    elif served == model:
        verdict = "not_triggered"
    elif served in chain:
        verdict = "failed_over"
    else:
        verdict = "unknown"
    return {
        "model": model,
        "chain": chain,
        "verdict": verdict,
        "served_by": served,
        "deployment_id": dep_id,
        "attempted_fallbacks": attempted,
    }
