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
- `core/drift.py`: config vs live, and node vs node.
- `core/lint.py`: built-in rules and the policy engine (operators are documented in the module docstring).
- `skills/cli-anything-litellm/SKILL.md` is the agent skill. `cli_anything/litellm/skills/SKILL.md` is a copy of it: keep them identical.

## Test
`pip install -e '.[dev]' && pytest && ruff check . && ruff format --check .`

Tests are offline. The fixtures reproduce response shapes seen on a real v1.100.1 proxy. When a live proxy behaves differently, capture the shape into a test before changing code.

## Rules
- Never echo secrets. `lint` masks anything secret-looking, and `config show` truncates the key.
- New mutating verbs go through `_dry` (dry run) and, if destructive, `_confirm`.
- Record behaviour changes in `CHANGELOG.md`.
