# Test documentation

All tests are offline: `pip install -e '.[dev]' && pytest`. No proxy is needed —
`be.get` / `be.post` are mocked, and the fixtures reproduce the response shapes
of a real LiteLLM v1.100.1 proxy.

## Layout

| File | Scope |
|---|---|
| `tests/test_harness.py` | Original build: core (`models`, `drift`, `lint`, `backend`) and CLI behaviours (dry-run, confirm, rotation, spend grouping, 401 hint). |
| `tests/test_core.py` | Unit tests for the newer cores: `models.new_deployment` / `models.resolve_model_target` (body construction, required args, id vs model-name match) and `users.new_user` / `users.update_user` / `users.resolve_user_target` / `users.normalize` (body + role validation, id vs unique-email resolution, ambiguous email refused). |
| `tests/test_full_e2e.py` | End-to-end and workflow tests: `models add` / `models delete` over the mocked API (`--dry-run` body == exact request body, `--yes` gating, replica counting, unknown-ref error), a `drift`-gap → `models add` → `models list --deployments` workflow, the delete-what-`drift`-found workflow, and the user-management set (`users list/info/create/update/delete` + the onboard→key→delete workflow). |

## DB-model management coverage (`models add` / `models delete`, v0.2.0)


- `models add` builds the `/model/new` body (`model_name`, `litellm_params`,
  optional `model_info`), requires `--name` and `--model`, supports `--dry-run`
  (prints the exact body, sends nothing) and reports the new deployment id.
- `models delete` resolves its argument against the live proxy: an exact
  `model_info.id` match deletes one replica; a model group name deletes every
  replica of it (the human output says how many). Without `--yes` off a TTY it
  refuses; an unknown ref is an error that points at `models list --deployments`.
- Workflow: a missing group is added via `models add` and shows up in
  `models list --deployments` as `SRC=db`.

## User management coverage (`users ...`, v0.3.0)

- `users list` follows `/user/get_users` pagination and filters client-side by
  `--role` / `--team`; `--json` rows are the `normalize` shape (id, email, role,
  teams, budget, spend, blocked).
- `users info <email>` resolves the email against the live list, then fetches
  `/user/info` by user id; the user's virtual keys go into its `--json` output.
- `users create` builds the `/user/new` body with `auto_create_key: false`
  (keys are minted deliberately via `keys generate --user`), requires
  `--user-id` or `--email`, and validates `--role` against LiteLLM's role list
  before the proxy is called; `--dry-run` prints the exact body and calls nothing.
- `users update` targets the ref (id, or email) and omits unset fields.
- `users delete` resolves its argument first: unknown ref → error pointing at
  `users list`, ambiguous shared email → refused, no TTY without `--yes` →
  refused. The confirmation names how many keys stop working, and the output
  lists them (`keys_removed`).
- Workflow: `users create` → `keys generate --user svc-bot` → `users list
  --team eng` shows the account → `users delete --yes` names the invalidated key.

## Results

`pytest tests --cov=cli_anything --cov-fail-under=70 -q` — 64 passed, 77.90% coverage (v0.3.0).
