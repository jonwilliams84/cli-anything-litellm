# Changelog

## [0.2.0] — 2026-10-03

DB-model management: the Admin UI's two mutations from the shell.

- **`models add`** — `POST /model/new` with `--name`, `--model` (LiteLLM provider
  string), `--api-base`, `--api-key` (use `os.environ/NAME` so the secret is
  resolved by the proxy, not stored in its DB), `--mode` and
  `--max-input-tokens`. `--dry-run` prints the exact request body; the response
  names the new deployment id.
- **`models delete`** — `POST /model/delete` for a deployment id (one replica)
  or a model group name (every replica of it; the output says how many). Needs
  `--yes` off a TTY, refuses an argument no live deployment matches, and suggests
  `models list --deployments`. Run `drift` first: replicas the config still
  describes come back on the next redeploy; DB-only ones do not.
- Core: `models.new_deployment` and `models.resolve_model_target`, unit-tested in
  `tests/test_core.py`; E2E and workflow tests in `tests/test_full_e2e.py`
  (a drift gap closed by `models add` then visible as `SRC=db` in `models list`).
- Docs: README, `TEST.md` and the SKILL (both copies) cover the new commands and
  the DB-model trap they pair with.

## [0.1.0] — 2026-09-25

First release, built and verified against a LiteLLM v1.100.1 proxy (27 deployments, custom guardrails, Postgres + Redis):

- `status`, `health`, `models list|consistency`, `router show`, `guardrails list`
- `keys list|info|generate|update|block|unblock|rotate|delete`, `teams list|info`, `spend`
- `drift`: config.yaml vs the live proxy.
  - Deployments are matched by `model_info.id`.
  - DB-only models (`db_model: true`) are flagged.
  - Router settings and guardrails are compared too.
  - Masked params (`thinking_token_budget` → `****`) are reported as unverifiable, not as drift.
- `fleet diff`: node vs node (version, deployments, router, guardrails, key visibility). A node that rejects the key is reported as a likely master-key mismatch.
- `lint`: built-in rules (literal secrets, placeholder credentials, duplicate or missing ids, replica mismatch, missing context limit, load-unaware routing) plus YAML site policies (`equals`, `contains`, `startswith`, `exists`, `in`, `min`, `max`, and `each_deployment` scoping).
- `keys rotate` implements rotation with open-source calls, because `/key/regenerate` is Enterprise-only.
