"""Tag-based spend attribution: keys carry tags, spend can be billed by them.

A *tag* is a free label on a virtual key — a feature ("feat-checkout"), a
project, a cost centre. It travels with every request the key makes: the proxy
copies the key's ``metadata.tags`` into its spend log, where the row's
``request_tags`` column records the tags that request carried. That makes tags
LiteLLM's chargeback dimension *between* keys and models: one key can serve
several features and be billed across them, and "how much does feature X cost"
is answerable without minting a key per feature.

The proxy has a tag *table* too (``/tag/*`` — Tag Management): a named tag
object that carries a description, the keys riding it, the models it is
allowed on and a ``budget_id`` — a budget the proxy applies to every request
carrying the tag, across all keys that share it. That is how one feature's cap
is set once when the feature rides several keys. The endpoints are
``GET /tag/list``, ``POST /tag/update`` (which is an upsert — there is no
``/tag/create``; updating a name not in the list creates its row) and
``POST /tag/delete`` (body ``{"tags": [...]}``). ``GET /tag/list`` returns
envelopes in more than one shape — a bare list, ``{"tags": [...]}``,
``{"data": [...]}``. Its items are the table rows as dicts, or plain tag-name
strings for config-declared names, so every reader tolerates both.

Shape notes (tolerated variants, as proxies store this in more than one way):

- The spend log's ``request_tags`` column is a *text* column: a JSON-encoded
  list on the proxies this was tested against, and a bare list or a
  comma-separated string on others. It may also be absent on rows whose
  request carried no tags.
- Key objects from ``/key/list`` put tags under ``metadata.tags``; older
  proxies flat-map them to ``tags``.

Unlike the other spend groupings (model/key/team/user), a row can carry
*several* tags at once — so the aggregate counts such a request under each of
its tags, and rows without any tags are reported once, as ``(untagged)``.
"""

from __future__ import annotations

import json
from typing import Any

UNTAGGED = "(untagged)"


def parse_tags(spec: str | list[str] | None) -> list[str]:
    """Parse a ``--tags`` value: comma-separated string or list → tag names.

    Strips whitespace, drops empty items, deduplicates preserving order.
    Raises ValueError when nothing usable remains, so "tag nothing but call it
    tagged" is refused before the proxy sees it.
    """
    if spec is None:
        return []
    items = spec if isinstance(spec, list) else [spec]
    out: list[str] = []
    for item in items:
        for t in str(item).split(","):
            t = t.strip()
            if t and t not in out:
                out.append(t)
    if not out:
        raise ValueError(f"--tags {spec!r}: no usable tag names")
    return out


def as_metadata(metadata: dict[str, Any] | None, tags: list[str]) -> dict[str, Any]:
    """Build the ``metadata`` object a /key/generate or /key/update call carries.

    Tags live at ``metadata.tags``; any other metadata keys on the key are
    preserved (the proxy replaces the whole object, so keeping them here is
    how a retag does not silently drop fields — or worse, a tag-only update
    from the Admin UI is not lost here either).
    """
    if not tags:
        raise ValueError("no tags to set — refuse to send an empty metadata.tags")
    return {**(metadata or {}), "tags": list(tags)}


def key_tags(key: dict[str, Any]) -> list[str]:
    """The tags a key object carries, sorted, as ``keys list`` shows them.

    ``metadata.tags`` on current proxies, flat ``tags`` on older shapes.
    """
    md = key.get("metadata")
    tags = md.get("tags") if isinstance(md, dict) else None
    raw = tags or key.get("tags") or []
    out: list[str] = []
    for t in raw if isinstance(raw, list) else [raw]:
        t = str(t).strip()
        if t and t not in out:
            out.append(t)
    return sorted(out)


def log_row_tags(row: dict[str, Any]) -> list[str]:
    """The tags recorded on one spend-log row, deduplicated preserving order.

    ``request_tags`` (a JSON-encoded text column — tolerate a list, a JSON
    string, a bare comma-separated string, or invalid JSON) wins, then
    ``metadata.tags``, then a flat ``tags``; absent everywhere → no tags.
    """
    out: list[str] = []
    for source in (row.get("request_tags"), (row.get("metadata") or {}).get("tags"), row.get("tags")):
        raw = source
        if raw is None or raw == []:
            continue
        if isinstance(raw, str):
            try:  # the text column stores JSON-encoded values; a bare comma string is not JSON
                raw = json.loads(raw)
            except ValueError:
                raw = raw  # fall through to the comma-split below
            if raw is None or raw == []:
                continue
        if isinstance(raw, str):
            raw = [p.strip() for p in raw.split(",")]
        if isinstance(raw, dict):
            raw = [k for k, v in raw.items() if v]
        for t in raw if isinstance(raw, list) else [raw]:
            t = str(t).strip()
            if t and t not in out:
                out.append(t)
    return out


def filter_by_tags(rows: list[dict[str, Any]], want: str) -> list[dict[str, Any]]:
    """Keep only rows whose tags include ``want`` (a row names all of its tags)."""
    return [r for r in rows if want in log_row_tags(r)]


def spend_by_tag(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Spend and request totals per tag, client-side from the paged spend log — the way
    ``spend --by model|key|team|user`` works, for the one grouping where a row
    can belong to a bucket *more than once* because several tags ride together.

    A row with no tags is counted once, as ``(untagged)``. Sorted by spend.
    """
    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        tags = log_row_tags(r) or [UNTAGGED]
        for tag in tags:
            a = agg.setdefault(
                tag,
                {"tag": tag, "requests": 0, "spend": 0.0, "prompt_tokens": 0, "completion_tokens": 0},
            )
            a["requests"] += 1
            a["spend"] += r.get("spend") or 0
            a["prompt_tokens"] += r.get("prompt_tokens") or 0
            a["completion_tokens"] += r.get("completion_tokens") or 0
    out = sorted(agg.values(), key=lambda a: (-a["spend"], a["tag"]))
    for a in out:
        a["spend"] = round(a["spend"], 4)
    return out


# ── tag management (/tag/*) ────────────────────────────────────────────────


def as_rows(res: Any) -> list[dict[str, Any]]:
    """Whatever ``/tag/list`` returned, as a list of tag rows.

    Proxies return a bare list, ``{"tags": [...]}`` or ``{"data": [...]}``;
    items are the table rows as dicts or plain tag-name strings
    (config-declared names). Strings become rows with only ``tag`` set.
    """
    items: Any = res
    if isinstance(res, dict):
        for key in ("tags", "data"):
            if isinstance(res.get(key), list):
                items = res[key]
                break
    if not isinstance(items, list):
        return []
    return [i if isinstance(i, dict) else {"tag": str(i)} for i in items]


def row_tag(row: dict[str, Any]) -> str | None:
    """The tag's name, on any shape the list returns (``tag``, ``tag_id``, ``name``)."""
    for key in ("tag", "tag_id", "name"):
        v = row.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def normalize(row: dict[str, Any]) -> dict[str, Any]:
    """One row per tag, as both the table and --json output show it.

    A config-declared tag is just a name; unknown fields are not invented.
    """
    tag = row_tag(row)
    return {
        "tag": tag,
        "description": row.get("description"),
        "models": row.get("models"),
        "keys": row.get("keys"),
        "budget_id": row.get("budget_id") or (row.get("litellm_budget_table") or {}).get("budget_id"),
    }


def resolve_tag(rows: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """Match ``ref`` against live tags, by exact name (tags have no alias).

    Returns ``{by, matches, tag}`` or None — the shape every resolver in this
    CLI returns, so the calling command can refuse an unknown reference it
    would otherwise let the proxy 400.
    """
    if not ref:
        return None
    hits = [r for r in rows if row_tag(r) == ref]
    if hits:
        return {"by": "tag", "matches": [ref], "row": hits[0]}
    return None


def _fields(spec: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in spec.items() if v is not None and v != [] and v != ""}


def update_body(
    current: dict[str, Any] | None,
    tag: str,
    description: str | None = None,
    models: list[str] | None = None,
    keys: list[str] | None = None,
    budget_id: str | None = None,
) -> dict[str, Any]:
    """Build the POST /tag/update body: the current row's fields plus the changes.

    ``/tag/update`` replaces the whole row (it is an upsert), so like
    ``keys update`` sending *only* the change would clear every field the row
    already carries — the current row (from ``/tag/list``) is merged in and
    each flag overrides. A name that is not in the list yet (``current`` None)
    creates the row: there is no ``/tag/create``, the update doubles as it.
    Raises ValueError when there is nothing to set — a body with just ``tag``
    is a no-op round trip.
    """
    if not tag or not tag.strip():
        raise ValueError("identify the tag with TAG — run `tags list`")
    changes = _fields({"description": description, "models": models, "keys": keys, "budget_id": budget_id})
    if not changes:
        raise ValueError("nothing to update: --description, --models, --keys or --budget-id")
    kept = (current or {}).get("litellm_budget_table") or {}
    base = _fields(
        {
            "description": (current or {}).get("description"),
            "models": (current or {}).get("models"),
            "keys": list((current or {}).get("keys") or []) if current else None,
            "budget_id": (current or {}).get("budget_id") or kept.get("budget_id"),
        }
    )
    base.pop("tag", None)
    return {"tag": tag, **base, **changes}


def delete_body(tags: list[str]) -> dict[str, Any]:
    """The POST /tag/delete body — the one endpoint in the family that is plural.

    The proxy removes the tag from the table and from every key and team
    riding it; it deletes no key.
    """
    tags = [t for t in tags if t and t.strip()]
    if not tags:
        raise ValueError("identify the tag with TAG — run `tags list`")
    return {"tags": tags}
