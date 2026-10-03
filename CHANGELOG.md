# Changelog

## [0.5.0] — 2026-10-03

Budget management: the fifth leg of spend control, next to keys, teams, users and spend.

A budget is a reusable cap in the proxy's database — a `max_budget` with an optional
`soft_budget` warning, a `budget_duration` reset cycle, rate/parallel limits and
per-model maxima (`model_max_budget`) — that keys reference by `budget_id`. The one
place to re-price fifty keys next quarter is the budget, not fifty key updates; but
until now the CLI could not even list those budgets, let alone change them. The new
`budgets` group covers the whole `/budget/*` family:

- **`budgets list`** — GET `/budget/list`. One row per budget: cap, soft cap, reset
  cycle, TPM/RPM/parallel limits and per-model caps. Tolerates both response shapes
  the proxy has used (a bare list, and the `{"data": [...]}` envelope).
- **`budgets info <budget_id>`** — GET `/budget/info`. The budget plus every key and
  team that references it, so you know what a change (or a delete) will hit.
- **`budgets create`** — POST `/budget/new` with `--budget-id` (what
  `keys generate --budget-id` references — the proxy generates one if omitted),
  `--max-budget`, `--soft-budget`, `--rpm`, `--tpm`, `--parallel`,
  `--duration` (the reset cycle, e.g. `30d` — the format
  `teams create --budget-duration` also takes) and `--model-max-budget` (a JSON
  object of per-model caps, e.g.
  `'{\"gpt-4o\": 0.01}'`). A budget with nothing to cap is refused client-side —
  a no-op budget would only be discovered in `budgets list`. `--dry-run` prints
  the exact body.
- **`budgets update <budget_id>`** — POST `/budget/update`. Given flags replace the
  field; omitted ones stay as-is. Applies immediately to every key, team and user
  on the budget, and that is the point: the command refuses an unknown id first
  (pointing at `budgets list`) rather than shipping a change that silently misses.
- **`budgets delete <budget_id>`** — POST `/budget/delete`. Destructive: needs
  `--yes` off a TTY. Says `{"id": ...}` — the one endpoint of the family that
  doesn't say `budget_id`. Attached keys, teams and users keep working with their
  own limits; they are detached, not deleted.
- Composition: `keys generate` and `keys update` gained `--budget-id`, and
  `keys rotate` now carries the budget id over to the replacement key — a rotation
  used to drop the shared cap and fall back to the key's inline limits.
- Core: `core/budgets.py` (`as_rows`, `normalize`, `new_budget`, `update_budget`,
  `resolve_budget_target`), unit-tested in `tests/test_core.py`; E2E and a
  create→attach→reprice→delete workflow in `tests/test_full_e2e.py`.
- Docs: README, `TEST.md` and the SKILL (both copies) cover the new commands and
  the budget reference they pair with.

## [0.4.0] — 2026-10-03

Team management: the fourth leg of identity, next to models, users and keys.

A team carries the budget, model access and rate limits that virtual keys
inherit and that users join, but until now it could only be created or changed
in the Admin UI. The `teams` group grows from read-only to full CRUD, all seven
`/team/*` mutation endpoints:

- **`teams create`** — POST `/team/new` with `--alias`, optional `--team-id`
  (the id is what `keys generate --team` and `users create --teams` reference,
  so pin it rather than let the proxy generate one), `--models`, `--max-budget`,
  `--rpm`, `--tpm`, `--budget-duration` and repeatable `--member` whose role is
  picked with `--member-role admin|user`. A member passed as an email gets a
  proxy account created for it; member roles are validated client-side, like
  user roles in 0.3.0. `--dry-run` prints the exact body.
- **`teams update <team_id|alias>`** — POST `/team/update`; given flags replace
  the field, omitted ones stay as-is. The argument is resolved against
  `/team/list`: exact `team_id` first, then a *unique* `team_alias`; an alias
  two teams share is refused, never guessed.
- **`teams delete <team_id|alias>`** — POST `/team/delete`. Destructive: needs
  `--yes` off a TTY. Deleting a team also deletes every virtual key minted for
  it — exactly the `users delete` trap — so the command names the keys that stop
  working (`keys_removed` in `--json`). Unknown ref → error pointing at
  `teams list`.
- **`teams member-add <team>` / `teams member-delete <team> --user`** — POST
  `/team/member_add` and `/team/member_delete`. member-add checks a bare user
  name against `users list` first (the endpoint 404s on an unknown user_id —
  pass their email instead, which the proxy creates); member-delete sends
  `user_id` or `user_email` per LiteLLM's request shape.
- **`teams block` / `teams unblock <team>`** — POST `/team/block` and
  `/team/unblock` (`{"team_ids": [id]}`). Blocking fails every key of the team,
  reversibly; the block needs `--yes` off a TTY.
- Core: `core/teams.py` (`new_team`, `update_team`, `normalize`,
  `resolve_team_target`, `member_entry`), unit-tested in `tests/test_core.py`;
  E2E and an onboard-team→team-key→delete-team workflow in
  `tests/test_full_e2e.py`.
- Docs: README, `TEST.md` and the SKILL (both copies) cover the new commands
  and the team-deletion trap they pair with.

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
