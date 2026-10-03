---
name: cli-anything-litellm
description: >-
  Administer, tune and audit a LiteLLM proxy from the shell: model groups and
  replicas, live router settings, guardrails, virtual keys (create, limit,
  block, rotate, delete), teams and spend. Detects DRIFT between the config.yaml
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
| Keys: issue / limit | `keys generate --alias A --models m1,m2 --duration 90d --max-budget 5 --rpm 60`, `keys update A --rpm 30` |
| Keys: stop / restore / replace / remove | `keys block A`, `keys unblock A`, `keys rotate A`, `keys delete A` |
| Add a model **as a DB row** (what the Admin UI does) | `models add --name G --model hosted_vllm/qwen --api-base http://n1:8000/v1 --api-key os.environ/K [--mode chat] [--max-input-tokens N]` |
| Remove a DB deployment (by id) or a whole group (by name) | `models delete <model|id>` (`drift` first — config-backed replicas come back on redeploy) |
| Teams | `teams list`, `teams info <team_id>` |
| Who is using it / what does it cost? | `spend --days 7 --by model|key|team|user` |

Mutations: always run with `--dry-run` first (prints the exact request, sends
nothing). `block`, `rotate`, `delete` and `models delete` need `--yes` when there is no TTY.

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
- **Placeholder credentials** (`api_key: none`) are reported by `lint` as
  `info`, not as leaked secrets: they mean the backend takes no key at all, which
  is worth a deliberate decision.

## Site policy files

`lint --help-policy` prints the format. A policy turns decisions an estate has
recorded (ADRs, runbooks) into checks, so an agent tuning the proxy cannot
quietly undo one: routing strategy, required pre-call checks, required
guardrails, advertised context per deployment, secrets from env. Keep the policy
file next to the config it governs, in the same repo.
