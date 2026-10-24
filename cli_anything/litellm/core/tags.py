"""Tag-based spend attribution: keys carry tags, spend can be billed by them.

A *tag* is a free label on a virtual key — a feature ("feat-checkout"), a
project, a cost centre. It travels with every request the key makes: the proxy
copies the key's ``metadata.tags`` into its spend log, where the row's
``request_tags`` column records the tags that request carried. That makes tags
LiteLLM's chargeback dimension *between* keys and models: one key can serve
several features and be billed across them, and "how much does feature X cost"
is answerable without minting a key per feature.

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
