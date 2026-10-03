# Changelog

## [0.3.0] — 2026-10-03

User management: the third leg of identity, next to teams and keys.

Every virtual key and spend row is attributed to a user, but the proxy's user
accounts were only manageable through the Admin UI — onboarding ("give svc-bot
an account, capped at $5") had to leave the shell. The new `users` group covers
all five `/user/*` endpoints:

- **`users list`** — GET `/user/get_users`, paginated like `keys list`, filterable
  with `--role` and `--team`. One row per user: role, teams, budget, spend, blocked.
- **`users info <name|email>`** — GET `/user/info`; an email is resolved to a
  user id first (a unique live match is required). Also lists the virtual keys
  minted for the user.
- **`users create`** — POST `/user/new` with `--user-id`/`--email`, `--role`
  (validated against LiteLLM's nine proxy roles before the proxy sees a typo),
  `--teams`, `--max-budget`, `--rpm`, `--tpm`. Always sets `auto_create_key:
  false`: a key minted implicitly has no alias, which is an audit dead end —
  issue keys deliberately with `keys generate --user <id>`. `--dry-run` prints
  the exact body.
- **`users update <name|email>`** — POST `/user/update`; given flags replace the
  field, omitted ones stay as-is.
- **`users delete <name|email>`** — POST `/user/delete`. Destructive: needs
  `--yes` off a TTY. Deleting a user also invalidates every key they own, so the
  command refuses an argument no live user matches (a shared email is refused
  too — delete by user id) and names the keys that stop working. Run `users
  info` first to see what is about to break.
- Core: `core/users.py` (`new_user`, `update_user`, `normalize`,
  `resolve_user_target`), unit-tested in `tests/test_core.py`; E2E and an
  onboard→key→delete workflow in `tests/test_full_e2e.py`.
- Docs: README, `TEST.md` and the SKILL (both copies) cover the new commands.

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
