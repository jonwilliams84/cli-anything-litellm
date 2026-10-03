# Test documentation

All tests are offline: `pip install -e '.[dev]' && pytest`. No proxy is needed —
`be.get` / `be.post` are mocked, and the fixtures reproduce the response shapes
of a real LiteLLM v1.100.1 proxy.

## Layout

| File | Scope |
|---|---|
| `tests/test_harness.py` | Original build: core (`models`, `drift`, `lint`, `backend`) and CLI behaviours (dry-run, confirm, rotation, spend grouping, 401 hint). |
| `tests/test_core.py` | Unit tests for the DB-model management core: `models.new_deployment` (body construction, required args) and `models.resolve_model_target` (id vs model-name match, `db_model` flag, unknown ref). |
| `tests/test_full_e2e.py` | End-to-end and workflow tests: `models add` / `models delete` over the mocked API (`--dry-run` body == exact request body, `--yes` gating, replica counting, unknown-ref error), a `drift`-gap → `models add` → `models list --deployments` workflow, and the delete-what-`drift`-found workflow. |

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

## Results

`pytest tests --cov=cli_anything --cov-fail-under=70 -q` — 40 passed (v0.2.0).
