# cli-anything-litellm

Click CLI over the LiteLLM proxy admin API. It is an HTTP client only: it never writes the proxy's config file.

## Layout
- `cli_anything/litellm/litellm_cli.py`: every command. Live readers (`live_deployments`, `live_router`, `live_guardrails`, `readiness`) are shared by `drift` and `fleet`.
- `core/backend.py`: transport and auth (flags > `LITELLM_URL`/`LITELLM_API_KEY` > `~/.cli-anything/litellm/config.json`), raising `ApiError(status, message, url)`.
- `core/models.py`: the deployment fingerprint, grouping and replica consistency.
- `core/teams.py`: `/team/*` mutation bodies and id-or-alias resolution.
- `core/users.py`: `/user/*` mutation bodies and id-or-email resolution.
- `core/budgets.py`: `/budget/*` bodies (delete says `id`, the rest say `budget_id`).
- `core/customers.py`: `/customer/*` bodies (block/unblock/delete say plural `user_ids`; new/update are singular).
- `core/logs.py`: per-request rows from the spend log, fetched paged from `/spend/logs/v2` by `litellm_cli._fetch_logs` (never the unpaged `/spend/logs?summarize=false`, which OOM-killed a 2Gi proxy; the log stores sha256(key), so callers are named via the key list or the row's own alias — never the hash), filters, latency percentiles.
- `core/drift.py`: config vs live, and node vs node.
- `core/usage.py`: server-side spend reports from `/global/spend/report` (the proxy's own DB aggregation) — window/`group_by` query, envelope tolerance, day rows summed client-side (`--by day` has no server-side form); api_key hashes are named via `logs.key_alias_map`.
- `core/tags.py`: keys carry `--tags` (stored as `metadata.tags`), `spend --by tag` bills per tag from the spend log's JSON-text `request_tags` column (several tags ride on one request; `(untagged)` for none), and `logs --tag` drills down.
- `core/mcp.py`: the MCP gateway (`/v1/mcp/*`): server rows, redaction (URL origin only, header/env values masked, credentials by field name — upstream keys hide in all of them), name/id resolution, POST/PUT bodies (PUT is partial: unset flags are omitted, never nulled), tool → server attribution, and keys' MCP grants from `object_permission` (merged per field by the proxy; `keys rotate` copies the grant, never the row id).
- `core/lint.py`: built-in rules and the policy engine (operators are documented in the module docstring).
- `skills/cli-anything-litellm/SKILL.md` is the agent skill. `cli_anything/litellm/skills/SKILL.md` is a copy of it: keep them identical.

## Test
`pip install -e '.[dev]' && pytest && ruff check . && ruff format --check .`

Tests are offline. The fixtures reproduce response shapes seen on a real v1.100.1 proxy; the MCP fixtures (`tests/mcp_fixtures.py`) reproduce v1.106.3 and cite the source function of each shape. When a live proxy behaves differently, capture the shape into a test before changing code.

## Rules
- Never echo secrets. `lint` masks anything secret-looking, and `config show` truncates the key.
- New mutating verbs go through `_dry` (dry run) and, if destructive, `_confirm`.
- Record behaviour changes in `CHANGELOG.md`.
