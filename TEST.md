# Test documentation

All tests are offline: `pip install -e '.[dev]' && pytest`. No proxy is needed —
`be.get` / `be.post` are mocked, and the fixtures reproduce the response shapes
of a real LiteLLM v1.100.1 proxy.

## Layout

| File | Scope |
|---|---|
| `tests/test_harness.py` | Original build: core (`models`, `drift`, `lint`, `backend`) and CLI behaviours (dry-run, confirm, rotation, spend grouping, 401 hint). |
| `tests/test_core.py` | Unit tests for the newer cores: `models.new_deployment` / `models.resolve_model_target` (body construction, required args, id vs model-name match), `users.new_user` / `users.update_user` / `users.resolve_user_target` / `users.normalize` (body + role validation, id vs unique-email resolution, ambiguous email refused), `teams.new_team` / `teams.update_team` / `teams.resolve_team_target` / `teams.normalize` / `teams.member_entry` (body + member-role validation, id vs unique-alias resolution, ambiguous alias refused), `budgets.as_rows` / `budgets.new_budget` / `budgets.update_budget` / `budgets.normalize` / `budgets.resolve_budget_target` (envelope tolerance, nothing-to-cap refusal, per-model caps via dict or JSON string, exact-id match) and `customers.as_rows` / `customers.new_customer` / `customers.update_customer` / `customers.normalize` / `customers.ids_body` / `customers.resolve_customer_target` (envelope tolerance, user_id-or-alias requirement, plural delete body, id vs unique-alias resolution, ambiguous alias refused). |
| `tests/test_full_e2e.py` | End-to-end and workflow tests: `models add` / `models delete` over the mocked API (`--dry-run` body == exact request body, `--yes` gating, replica counting, unknown-ref error), a `drift`-gap → `models add` → `models list --deployments` workflow, the delete-what-`drift`-found workflow, the user-management set (`users list/info/create/update/delete` + the onboard→key→delete workflow), the team-management set (`teams create/update/delete/member-add/member-delete/block/unblock` + the team→key→delete workflow), the budget-management set (`budgets list/info/create/update/delete` + the create→attach→reprice→delete workflow) and the customer-management set (`customers list/info/create/update/block/unblock/delete` + the onboard→budget→block→unblock→delete workflow). |

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

## Team management coverage (`teams ...`, v0.4.0)

- `teams list` rows are the `core/teams.normalize` shape (id, alias, members,
  spend, budget, rpm/tpm, models, blocked) — the `--json` shape grew
  `rpm_limit`/`tpm_limit`; the human table is unchanged.
- `teams create --alias A` builds the `/team/new` body: optional `--team-id`
  (the id is what `keys generate --team` and `users create --teams` reference,
  so pinning one is usually worth it), `--models`, `--max-budget`, `--rpm`,
  `--tpm`, `--budget-duration`, and `--member` (repeatable, name or email;
  emails get a proxy account created). `--member-role` is validated against
  LiteLLM's `admin|user` before the proxy is called; `--dry-run` prints the
  exact body and calls nothing.
- `teams update <team_id|alias>` resolves the ref against `/team/list` (exact
  team_id first, then a *unique* alias — an ambiguous alias is refused),
  requires something to change, and omits unset fields.
- `teams delete <team_id|alias>` resolves the ref first, sends
  `{"team_ids": [id]}`, and — like `users delete` — refuses without `--yes` off
  a TTY and names the keys the team owned (they stop working: `keys_removed`).
  An unknown ref errors and points at `teams list`.
- `teams member-add --user U-or-E` sends `/team/member_add` with
  `{"team_id", "member": [...]}`; a bare name is resolved against `users list`
  (unknown → error, use the email so the proxy can create the account), an
  email is passed through as `user_email`.
- `teams member-delete --user U-or-E` sends `/team/member_delete` with
  `{"team_id", "user_id"|"user_email"}` at the top level.
- `teams block` / `teams unblock` send `/team/block|/team/unblock` with
  `{"team_ids": [id]}`; block is destructive-gated (`--yes` off a TTY).
- Workflow: `teams create --team-id team-eng` → `keys generate --team team-eng`
  → `teams list` shows the member count → `teams delete --yes` names the
  invalidated key.

## Budget management coverage (`budgets ...`, v0.5.0)

- `budgets list` reads `/budget/list` and tolerates both response shapes the
  proxy has used — a bare list and the `{"data": [...]}` envelope
  (`core/budgets.as_rows`); `--json` rows are the `normalize` shape (budget_id,
  caps, reset cycle, tpm/rpm/parallel, per-model caps).
- `budgets info <budget_id>` fetches `/budget/info` and names the budget plus
  every key and team referencing it — what a change or delete will hit.
- `budgets create` builds the `/budget/new` body: optional `--budget-id` (the id
  `keys generate --budget-id` references), `--max-budget`, `--soft-budget`,
  `--rpm`, `--tpm`, `--parallel`, `--duration` (reset cycle, e.g. `30d`) and
  `--model-max-budget` (JSON object of per-model caps — parse errors and
  non-object JSON are refused client-side). A budget with nothing to cap is a
  ValueError before the proxy is called. `--dry-run` prints the exact body.
- `budgets update <budget_id>` resolves the id against `/budget/list` (unknown →
  error pointing at `budgets list`), requires something to change, omits unset
  fields.
- `budgets delete <budget_id>` sends `{"id": ...}` — the one `/budget/*`
  endpoint that does not say `budget_id`. Refuses without `--yes` off a TTY;
  `--dry-run` prints the body first.
- Composition: `keys generate --budget-id` / `keys update --budget-id` attach a
  shared budget, and `keys rotate` carries `budget_id` over to the key.
- Workflow: `budgets create` → `keys generate --budget-id` → `budgets info`
  shows the attached key → `budgets update` re-prices the fleet in one call →
  `budgets delete --yes`.

## Customer management coverage (`customers ...`, v0.6.0)

- `customers list` reads `/customer/list`, follows its pagination when the
  proxy sends the `{"customers": [...], "total_pages": ...}` envelope, and
  tolerates the bare-list shape older proxies return
  (`core/customers.as_rows`); `--json` rows are the `normalize` shape
  (user_id, alias, spend, budget, caps, blocked) and `--budget ID` filters
  client-side.
- `customers info <user_id|alias>` resolves an alias against the live list
  first (`/customer/info` is keyed by `user_id` alone).
- `customers create` builds the `/customer/new` body: `--user-id` (what the
  app passes with each request), `--alias`, `--max-budget`, `--budget-id`
  (a shared budget), `--rpm`, `--tpm`. A customer with neither a user id nor
  an alias is refused client-side — the proxy would generate an id nobody
  knows. `--dry-run` prints the exact body.
- `customers update <user_id|alias>` resolves the ref first (unknown → error
  pointing at `customers list`), requires something to change, and omits
  unset fields.
- `customers block` / `customers unblock` send `/customer/block` /
  `/customer/unblock` with `{"user_ids": [id]}`; block is destructive-gated
  (`--yes` off a TTY).
- `customers delete <user_id|alias>` sends `{"user_ids": [id]}` — the
  plural body the family's write endpoints share (`ids_body`), unlike the
  budget family whose delete alone says `id`. Refuses without `--yes` off a
  TTY; deleting a customer stops spend attribution for that end user and
  deletes no key.
- Workflow: `customers create --budget-id` → `customers list --budget` shows
  the customer on the cap → `customers block` → `customers info` shows
  `blocked: true` → `customers unblock` → `customers delete --yes`.

## Results

`pytest tests --cov=cli_anything --cov-fail-under=70 -q` — 134 passed, 84.6% coverage (v0.6.0).
