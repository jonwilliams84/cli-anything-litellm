---
name: cli-anything-litellm
description: >-
  Administer, tune and audit a LiteLLM proxy from the shell: model groups and
  replicas, live router settings, guardrails, virtual keys (create, limit,
  block, rotate, delete), user accounts (create, update, delete), teams
  (create, update, members, block, delete), budgets (reusable spend caps keys
  reference by budget_id: create, update, delete) and spend. Detects DRIFT between
  the config.yaml
  in git and the running proxy (including models added through the Admin UI that
  exist only in its database), differences between proxies behind one VIP (the
  mismatched-master-key 401 class), and lints a config against built-in hygiene
  rules plus a site POLICY file of recorded decisions. Use before and after any
  LiteLLM change, and whenever inference "got slow" or clients see 401s.
---

# cli-anything-litellm

A stateless CLI over the LiteLLM proxy's admin API. It never edits the proxy's
config file: the config in git is the source of truth, and this tool tells you
whether the running proxy still matches it.

## Connect

```bash
export LITELLM_URL=https://litellm.example        # or --url
export LITELLM_API_KEY=sk-...                     # master/admin key; or --key
export LITELLM_CA_BUNDLE=/path/to/internal-ca.pem # internal CA, if any
cli-anything-litellm config test                  # reachability + auth + version
```

`config set --url … [--key …]` saves to `~/.cli-anything/litellm/config.json`
(mode 600). Prefer the env var for the key. Every command takes `--json`.

## Which command

| Question | Command |
|---|---|
| Is it up? Which version, DB, cache, callbacks? | `status` |
| Are the backends answering? | `health [--model M]` — sends a REAL request to every deployment |
| What models/replicas does it serve? | `models list` (`--deployments` for one row per replica; `SRC=db` = added via UI/API) |
| Are replicas of one group interchangeable? | `models consistency` |
| What is the router actually doing? | `router show` (`--all` for every field) |
| Which guardrails are loaded, for whom? | `guardrails list` |
| **Does the live proxy match git?** | `drift --config path/to/config.yaml` (exit 2 = drift) |
| **Would this config change break a recorded decision?** | `lint --config … --policy policy.yaml` (exit 1 = errors) |
| **Are the nodes behind the VIP identical?** | `fleet diff --node URL1 --node URL2` (exit 2 = differ) |
| Keys: list / inspect | `keys list [--team T]`, `keys info <alias|sk-…|hash>` |
| Keys: issue / limit | `keys generate --alias A --models m1,m2 --duration 90d --max-budget 5 --rpm 60` (or `--budget-id B` to inherit a shared budget), `keys update A --rpm 30` |
| Keys: stop / restore / replace / remove | `keys block A`, `keys unblock A`, `keys rotate A` (carries `budget_id` over), `keys delete A` |
| Budgets: list / inspect | `budgets list`, `budgets info <budget_id>` (also names every key and team on it) |
| Budgets: create / re-price / remove | `budgets create --budget-id B --max-budget 100 --duration 30d [--soft-budget 90] [--parallel 5] [--model-max-budget '{"gpt-4o": 0.01}']`, `budgets update B --max-budget 200` (hit every attached key, team and user at once), `budgets delete B --yes` (attached keys/teams keep working on their own limits) |
| Add a model **as a DB row** (what the Admin UI does) | `models add --name G --model hosted_vllm/qwen --api-base http://n1:8000/v1 --api-key os.environ/K [--mode chat] [--max-input-tokens N]` |
| Remove a DB deployment (by id) or a whole group (by name) | `models delete <model|id>` (`drift` first — config-backed replicas come back on redeploy) |
| Teams: list / inspect | `teams list`, `teams info <team_id>` |
| Teams: create / change | `teams create --alias A --team-id T [--models m1,m2] [--max-budget 50] [--member U|--member E]`, `teams update <team_id|alias> --models m1,m2 [--max-budget N]` |
| Teams: add / remove a member | `teams member-add <team_id|alias> --user U-or-E [--role admin|user]`, `teams member-delete <team_id|alias> --user U-or-E` |
| Teams: stop / restore / remove | `teams block <team_id|alias> --yes`, `teams unblock <team_id|alias> --yes`, `teams delete <team_id|alias> --yes` (also deletes every key minted for the team) |
| User accounts: list / inspect | `users list [--role R] [--team T]`, `users info <user_id|email>` (also lists the user's keys) |
| User accounts: create | `users create --user-id U [--email E] [--role proxy_admin_viewer] [--teams a,b] [--max-budget 5]` — never mints a key; issue one with `keys generate --user U` |
| User accounts: change / remove | `users update U --teams a,b [--role R] [--max-budget N]`, `users delete U --yes` (also invalidates every key the user owns — run `users info` first) |
| Who is using it / what does it cost? | `spend --days 7 --by model|key|team|user` |

Mutations: always run with `--dry-run` first (prints the exact request, sends
nothing). `keys block`, `keys rotate`, `keys delete`, `models delete`,
`users delete`, `teams block`, `teams delete`, `budgets delete` need `--yes`
when there is no TTY.

## Workflow for any change

1. `lint --config <new config> --policy <site policy>` — before deploying.
2. Deploy the way the estate deploys (never via this tool).
3. `drift --config <same file>` against **every** node, then `fleet diff`.
4. `health` if backends changed.

## Traps this tool already handles (don't rediscover them)

- **Masked params.** `/model/info` masks any value whose key *contains* a
  sensitive word — including harmless ones like `extra_body.thinking_token_budget`
  (`"****"`). `drift` does not call these differences; it lists them under
  `unverifiable_masked_params`. They are NOT verified — check the file on the node
  if they matter.
- **DB-only models.** Models added through the Admin UI or `/model/new` (needs
  `STORE_MODEL_IN_DB`) live in the database, not the file. They survive a config
  redeploy and never appear in git. `drift` reports them as `only_live` with
  `db_model: true`. Manage them from the shell: `models add` (POST /model/new —
  pass `--api-key os.environ/NAME` so the secret stays out of the proxy's DB) and
  `models delete` (POST /model/delete — a `model_info.id` removes one replica, a
  model group name removes every replica of it).
- **401 behind a VIP.** If only some requests 401, the nodes have different
  master keys. `fleet diff` names the node that rejects the key.
- **Version / cache / callbacks** moved to the authenticated
  `/health/readiness/details` in ~1.100; plain `/health/readiness` returns only
  status + db. `status` handles both.
- **`/key/regenerate` is an Enterprise feature** (HTTP 500 on the open-source
  proxy). `keys rotate` does the equivalent in three open-source calls: rename the
  old key, generate a replacement with the same alias/team/models/limits, delete
  the old one. The old secret stops working at the last step.
- **Spend logs are written asynchronously.** Right after traffic, `spend` can be
  short; wait a few seconds. Deleted keys are still named in `spend --by key`
  (the alias is recorded in each log row); the master key shows as `(master key)`.
- **`health` is not free.** It sends a completion to every deployment. On a
  metered upstream that costs money; on local vLLM it adds load.
- **`teams delete` deletes every key minted for the team**, exactly like
  `users delete` invalidates a user's keys — the confirmation names them, so
  check `keys list --team <team_id>` before deleting. Blocking a team fails all
  of its keys too, but reversibly. Delete matches an exact `team_id` first, then
  a *unique* `team_alias`; an alias two teams share is refused (delete by id).
  Pass `--member E` with an email and the proxy creates that account; member-add
  by bare name is resolved against `users list` first (unknown name → error).
- **`users create` never mints a key.** It sends `auto_create_key: false`, because
  an implicitly minted key has no alias and hides in audits. Keys are issued on
  purpose: `keys generate --alias svc --user svc-bot`. Conversely `users delete`
  invalidates every key the user owns — the confirmation names them, so check
  `users info` before deleting a shared account.
- **Roles are LiteLLM's, validated client-side**: `proxy_admin*`, `internal_user*`,
  `team`, `customer`. A typo fails before the proxy's opaque 400 does; `proxy_*`
  roles can mutate the whole proxy — grant them sparingly.
- **Budgets are the shared way to cap keys.** `budgets create --budget-id B`
  then `keys generate --budget-id B`: the cap, reset cycle and per-model maxima
  live in one place, so `budgets update B --max-budget 200` re-prices every
  attached key, team and user at once instead of fifty `keys update` calls.
  `/budget/delete` sends `{"id": ...}` — the one `/budget/*` endpoint that does
  not say `budget_id`. Deleting a budget detaches its keys, teams and users;
  they keep working under their own inline limits. A `budgets create` with
  nothing to cap is refused before the proxy is called.
- **Placeholder credentials** (`api_key: none`) are reported by `lint` as
  `info`, not as leaked secrets: they mean the backend takes no key at all, which
  is worth a deliberate decision.

## Site policy files

`lint --help-policy` prints the format. A policy turns decisions an estate has
recorded (ADRs, runbooks) into checks, so an agent tuning the proxy cannot
quietly undo one: routing strategy, required pre-call checks, required
guardrails, advertised context per deployment, secrets from env. Keep the policy
file next to the config it governs, in the same repo.
