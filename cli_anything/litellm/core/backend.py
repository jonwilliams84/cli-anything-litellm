"""HTTP transport for the LiteLLM proxy admin API.

Connection settings resolve flags > env > saved config (see ``resolve_conn``).
Every call raises :class:`ApiError` with the HTTP status and LiteLLM's own
error text, so the CLI can say *why* (a 401 on one gateway of two usually
means the master keys differ) instead of dumping a traceback.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests

CONFIG_PATH = Path(
    os.environ.get("CLI_ANYTHING_LITELLM_CONFIG", "~/.cli-anything/litellm/config.json")
).expanduser()


class ApiError(Exception):
    def __init__(self, status: int | None, message: str, url: str):
        self.status, self.message, self.url = status, message, url
        super().__init__(f"{status or 'no response'} from {url}: {message}")


def load_saved() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save(conf: dict[str, Any]) -> Path:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(conf, indent=2))
    CONFIG_PATH.chmod(0o600)
    return CONFIG_PATH


def resolve_conn(url: str | None = None, key: str | None = None) -> dict[str, Any]:
    """flags > env (LITELLM_URL / LITELLM_API_KEY) > saved config."""
    saved = load_saved()
    verify: bool | str = True
    if os.environ.get("LITELLM_CA_BUNDLE") or saved.get("ca_bundle"):
        verify = os.environ.get("LITELLM_CA_BUNDLE") or saved["ca_bundle"]
    if os.environ.get("LITELLM_VERIFY_SSL", "").lower() in ("0", "false", "no"):
        verify = False
    return {
        "url": (url or os.environ.get("LITELLM_URL") or saved.get("url") or "").rstrip("/"),
        "key": key
        or os.environ.get("LITELLM_API_KEY")
        or os.environ.get("LITELLM_MASTER_KEY")
        or saved.get("key"),
        "verify": verify,
        "timeout": float(os.environ.get("LITELLM_TIMEOUT", saved.get("timeout", 30))),
    }


def _error_text(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    err = body.get("error", body.get("detail", body)) if isinstance(body, dict) else body
    if isinstance(err, dict):
        return str(err.get("message") or err.get("error") or err)[:300]
    return str(err)[:300]


def request(
    conn: dict[str, Any],
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: Any = None,
    auth: bool = True,
) -> Any:
    if not conn.get("url"):
        raise ApiError(None, "no proxy URL: pass --url, set LITELLM_URL, or `config set`", path)
    url = f"{conn['url']}{path}"
    headers = {"Accept": "application/json"}
    if auth:
        if not conn.get("key"):
            raise ApiError(None, "no key: pass --key, set LITELLM_API_KEY, or `config set`", url)
        headers["Authorization"] = f"Bearer {conn['key']}"
    try:
        resp = requests.request(
            method,
            url,
            params=params,
            json=body,
            headers=headers,
            verify=conn.get("verify", True),
            timeout=conn.get("timeout", 30),
        )
    except requests.RequestException as exc:
        raise ApiError(None, f"{type(exc).__name__}: {exc}", url) from exc
    if resp.status_code >= 400:
        raise ApiError(resp.status_code, _error_text(resp), url)
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


def get(conn, path, **kw):
    return request(conn, "GET", path, **kw)


def post(conn, path, body=None, **kw):
    return request(conn, "POST", path, body=body, **kw)
