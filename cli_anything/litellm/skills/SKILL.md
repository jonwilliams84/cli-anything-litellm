---
name: cli-anything-litellm
description: >-
  Administer, tune and audit a LiteLLM proxy from the shell: model groups and
  replicas, live router settings, routing resilience (fallback chains: list,
  set, delete, and test a chain by failing a group on purpose; pause/unpause
  one DB deployment; recorded health — the proxy's own checks, latest and
  history, without calling any backend), guardrails, virtual keys (create, limit,
  tag, block, rotate, delete), user accounts (create, update, delete), teams
  (create, update, members, block, delete), budgets (reusable spend caps keys
  reference by budget_id: create, update, delete), customers (end users of the
  app behind the proxy: track and cap by user_id, block, delete), per-request
  logs from the spend log (who called what, at what spend/latency, filterable
  by key/user/team/model/tag), spend (grouped by model/key/team/user/tag) and
  tags on keys for per-feature chargeback; usage report — server-side spend
  reports the proxy aggregates in its own
  database (/global/spend/report: per
  day/team/key/model/customer over any window, even one a raw-log download
  could not carry); the MCP gateway — MCP servers the proxy fronts (list,
  health, tools, access groups, add/update/delete DB servers, live gateway
  sessions) and which keys reach which servers (keys --mcp-groups/--mcp-servers,
  mcp access); guardrails — list config and DB guardrails, add/update/delete
  DB ones, test one on a text, usage (evaluated/blocked/fail rate per
  guardrail), the requests a guardrail judged, and the team submission queue.
  The response cache — health (ping, plus the Redis under it: memory, hit
  rate, evictions), the stored settings (show, test, update — secrets as
  os.environ/ refs), targeted invalidation by x-litellm-cache-key and
  flushall. Detects DRIFT between
  the config.yaml
  in git and the running proxy (including models added through the Admin UI that
  exist only in its database, and fallback rules set through the API), differences between proxies behind one VIP (the
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
| What did the proxy's own health checks last see? (free) | `health latest [--unhealthy]` — no backend called; `gone` = row outlived its deployment, `blocked` = paused, `unknown` = never checked; exit 2 if a live, unpaused deployment is unhealthy |
| When did a backend go down / come back? | `health history [--model GROUP] [--status unhealthy] [--limit ≤1000] [--offset N]` — background checks record status CHANGES only |
| Take one bad replica out of rotation (reversibly) | `models block <deployment-id> --yes` (DB deployments only; says how many replicas keep serving), `models unblock <id>`; `models list --deployments` shows PAUSED |
| What happens when a group fails? | `fallbacks list [--model M] [--type general|context_window|content_policy]` (`SRC=config|db`), `fallbacks show <model>` (the effective chain, `*` catch-all included; `resolved_by: proxy` or `router_settings` for names with `/`) |
| Add / change / remove a fallback rule | `fallbacks set <model> --to a,b [--type T]`, `fallbacks delete <model> --yes` — refused when config.yaml owns that list; `delete` cannot address a name with `/` (see traps) |
| Does the fallback chain actually serve? | `fallbacks test <model> [--type T]` — fails the primary on purpose (the router's mock flag); `failed_over` (exit 0) / `chain_failed` / `not_triggered` / `unknown` (exit 2). One REAL completion, logged and billed |
| What models/replicas does it serve? | `models list` (`--deployments` for one row per replica; `SRC=db` = added via UI/API) |
| Are replicas of one group interchangeable? | `models consistency` |
| What is the router actually doing? | `router show` (`--all` for every field) |
| Which guardrails are loaded, for whom? | `guardrails list` (config.yaml — what `drift` compares), `guardrails list --all` (+ DB guardrails, `SRC=config|db`, ids), `guardrails info <id|name>` (secrets masked) |
| Does a guardrail do what I think? | `guardrails test <name> --text '...'` (or stdin) — passed / modified (prints the rewrite) / blocked (exit 3). A real request: logged, may cost on a paid guardrail service |
| Are guardrails firing too much? | `guardrails usage [--days 7 | --start --end]` (exit 2 if one is `critical`, >15% blocked), `guardrails usage <name>` (daily series), `guardrails logs <name> [--action blocked] [--snippets]` |
| Add / change / remove a DB guardrail | `guardrails add --name N --guardrail presidio --mode pre_call[,post_call] [--default-on] [--api-base U] [--api-key os.environ/VAR] [--param k=v]`, `guardrails update <name> --no-default-on`, `guardrails delete <name> --yes` |
| Team guardrail submissions | `guardrails submissions [--status pending_review]`, `guardrails approve <id> --yes`, `guardrails reject <id>` |
| **Does the live proxy match git?** | `drift --config path/to/config.yaml` (exit 2 = drift) |
| **Would this config change break a recorded decision?** | `lint --config … --policy policy.yaml` (exit 1 = errors) |
| **Are the nodes behind the VIP identical?** | `fleet diff --node URL1 --node URL2` (exit 2 = differ) |
| Keys: list / inspect | `keys list [--team T]`, `keys info <alias|sk-…|hash>` |
| Keys: issue / limit / tag | `keys generate --alias A --models m1,m2 --duration 90d --max-budget 5 --rpm 60` (or `--budget-id B` to inherit a shared budget), `keys update A --rpm 30`, `keys generate|update --tags feat-x,team-a` (tags are the chargeback labels `spend --by tag` bills by) |
| Keys: stop / restore / replace / remove | `keys block A`, `keys unblock A`, `keys rotate A` (carries `budget_id` over), `keys delete A` |
| Budgets: list / inspect | `budgets list`, `budgets info <budget_id>` (also names every key and team on it) |
| Budgets: create / re-price / remove | `budgets create --budget-id B --max-budget 100 --duration 30d [--soft-budget 90] [--parallel 5] [--model-max-budget '{"gpt-4o": 0.01}']`, `budgets update B --max-budget 200` (hit every attached key, team and user at once), `budgets delete B --yes` (attached keys/teams keep working on their own limits) |
| Customers (end users): list / inspect | `customers list [--budget B]`, `customers info <user_id|alias>` |
| Customers: create / change | `customers create --user-id U [--alias A] [--max-budget N] [--budget-id B] [--rpm N] [--tpm N]`, `customers update <user_id|alias> [--max-budget N] [--budget-id B]` |
| Customers: stop / restore / remove | `customers block <user_id|alias> --yes`, `customers unblock <user_id|alias>`, `customers delete <user_id|alias> --yes` (stops spend attribution for the end user; deletes no key) |
| Add a model **as a DB row** (what the Admin UI does) | `models add --name G --model hosted_vllm/qwen --api-base http://n1:8000/v1 --api-key os.environ/K [--mode chat] [--max-input-tokens N]` |
| Remove a DB deployment (by id) or a whole group (by name) | `models delete <model|id>` (`drift` first — config-backed replicas come back on redeploy) |
| Teams: list / inspect | `teams list`, `teams info <team_id>` |
| Teams: create / change | `teams create --alias A --team-id T [--models m1,m2] [--max-budget 50] [--member U|--member E]`, `teams update <team_id|alias> --models m1,m2 [--max-budget N]` |
| Teams: add / remove a member | `teams member-add <team_id|alias> --user U-or-E [--role admin|user]`, `teams member-delete <team_id|alias> --user U-or-E` |
| Teams: stop / restore / remove | `teams block <team_id|alias> --yes`, `teams unblock <team_id|alias>`, `teams delete <team_id|alias> --yes` (also deletes every key minted for the team) |
| User accounts: list / inspect | `users list [--role R] [--team T]`, `users info <user_id|email>` (also lists the user's keys) |
| User accounts: create | `users create --user-id U [--email E] [--role proxy_admin_viewer] [--teams a,b] [--max-budget 5]` — never mints a key; issue one with `keys generate --user U` |
| User accounts: change / remove | `users update U --teams a,b [--role R] [--max-budget N]`, `users delete U --yes` (also invalidates every key the user owns — run `users info` first) |
| Who is using it / what does it cost? | `spend --days 7 --by model|key|team|user|tag` |
| How much does one feature (or project, or cost centre) cost? | `spend --by tag` — keys tagged with `keys generate|update --tags`; a request riding several tags is counted under each, `(untagged)` for none |
| Which requests are behind that number? Who called what, at what latency? | `logs list [--key svc|sk-...|--user U|--team T|--model M|--tag T] [--days 7] [--limit 25]` — one row per request, newest first (tokens, spend, latency, cache hit) |
| Is traffic healthy: throughput, latency tail, cache hits per model? | `logs summary [--key ...|--user ...|--team ...|--model M|--tag T] [--days 7]` |
| MCP: which servers does the gateway front, are they up? | `mcp list [--health]` (`SRC=config` = config.yaml, read-only here; `db` = added via API/UI), `mcp health [SERVER...]` (exit 2 = one is unhealthy), `mcp info <id|alias|name>` (secrets redacted) |
| MCP: what tools does a client see? | `mcp tools [--server S]` — for the key the CLI runs as; `mcp groups` — each access group and its servers |
| MCP: which keys reach which servers? | `mcp access [--server S] [--team T]` — from each key's own `object_permission`; `(inherited: team:…/org:…/role:…)` only where one could grant servers, otherwise `(none)` (the gateway denies such a key) |
| MCP: grant / revoke a key's servers | `keys generate|update K --mcp-groups devtools,home` / `--mcp-servers ha,grafana` (`none` = no MCP at all; `''` clears groups) |
| MCP: add / change / remove a DB server | `mcp add --name N --url U [--alias A] [--access-groups g] [--auth-type bearer_token --auth-value-env VAR]`, `mcp update S --access-groups g`, `mcp delete S --yes` |
| MCP: who is connected right now? kick a session | `mcp sessions` (per worker process), `mcp sessions-close --user U|--prefix P --yes` |
| Is the response cache alive? What is it? | `cache ping` — status, type, ping and a test write (it is NOT a pure read); `--json` carries the cache's own params, masked; exit 2 when down or absent |
| Is the Redis under the cache healthy? | `cache info` — memory, maxmemory policy, hit rate, evictions, keyspace, total keys (`--json` keeps the full INFO dict and client list) |
| What are the stored cache settings? | `cache settings` — the DB row + REDIS_* env the proxy reports, credentials as `***REDACTED***`; a cache configured in config.yaml does NOT appear here, and it warns when these settings name a different Redis from the live cache |
| Change the cache (ttl, host, credentials)? | `cache settings test --set k=v …` (connects, never saves), then `cache settings update --set k=v …` — changes merge over the effective settings; secrets must be `os.environ/NAME`; warns when a target change drops the stored credential |
| A client keeps getting a stale answer | `cache delete <key> --yes` — the key is the response's `x-litellm-cache-key` header; the next request re-hits the backend. `cache flushall --yes` drops everything |
| What did the month cost (a window too wide to download raw logs for)? | `usage report [--by day|team|api_key|model|customer] [--days 7]` or `--start YYYY-MM-DD --end YYYY-MM-DD` — the proxy's own DB aggregation behind the Admin UI's charts; api_key rows are named exactly like `spend --by key` |

Mutations: always run with `--dry-run` first (prints the exact request, sends
nothing). `keys block`, `keys rotate`, `keys delete`, `models delete`,
`users delete`, `teams block`, `teams delete`, `budgets delete`, `customers block`, `customers delete`,
`mcp delete`, `mcp sessions-close`, `guardrails delete`, `guardrails approve`,
`models block`, `fallbacks delete`, `cache delete`, `cache flushall`
need `--yes` when there is no TTY (`cache settings update` needs it only to
pin a row over a config-owned cache — see traps).

## Workflow for any change

1. `lint --config <new config> --policy <site policy>` — before deploying.
2. Deploy the way the estate deploys (never via this tool).
3. `drift --config <same file>` against **every** node, then `fleet diff`.
4. `health latest` (free), or `health` if backends changed and you accept a real request each.

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
- **Spend logs are written asynchronously.** Right after traffic, `spend`,
  `logs list` and `logs summary` can be short; wait a few seconds. Deleted keys
  are still named in `spend --by key` (the alias is recorded in each log row);
  the master key shows as `(master key)`. `logs list --key` takes an alias, an
  `sk-...` key (hashed before matching — the log stores sha256, never the secret)
  or a hashed token; the row's own `api_key` is that hash and is never echoed —
  callers are named from the live key list or the alias recorded at request time
  (`svc (gone)` for renamed, rotated or deleted keys). The window filter is
  server-side (`--days`); key/user/team/model filters are applied client-side.
- **`health` is not free.** It sends a completion to every deployment. On a
  metered upstream that costs money; on local vLLM it adds load. `health
  latest` / `health history` read the proxy's recorded checks instead.
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
- **Customers are the app's end users**, identified by the `user_id` the app
  passes with every request; spend is attributed to them live and capped by
  their inline `max_budget` or a shared `budget_id`. The mutating endpoints of
  the family (block / unblock / delete) all take `{"user_ids": [...]}`. Delete
  stops spend attribution for that end user — it deletes no key. A
  `customers create` without a user id or an alias is refused client-side: the
  proxy would generate an id nobody can find later.
- **Placeholder credentials** (`api_key: none`) are reported by `lint` as
  `info`, not as leaked secrets: they mean the backend takes no key at all, which
  is worth a deliberate decision.
- **Tags are the chargeback dimension between keys and models.** `keys
  generate --tags feat-x,team-a` stores them at `metadata.tags` (older proxies
  flat-map them to `tags` — `keys list` tolerates both) and the proxy copies
  them into the spend log's `request_tags` column, which is *text*: a
  JSON-encoded list on current proxies, a bare list or comma-separated string
  on others. A request is billed under every tag it carried, so one key can
  serve several features; requests with no tags land in `(untagged)`.
  `keys update --tags` replaces the tag set and keeps the rest of the key's
  metadata (`/key/update` otherwise replaces the whole object), and `keys
  rotate` carries the tags over to the replacement key.

- **MCP servers have two sources, like models.** `mcp_servers:` in config.yaml
  shows as `SRC=config` (`is_config: true`): the API cannot change or delete it,
  so `mcp update` / `mcp delete` refuse and point at the config. `mcp add`
  writes a DB row that survives redeploys and is not in git. Tool names are
  `<alias-or-server_name>-<tool>`, which is why the proxy (and `mcp add`)
  refuses a `-` in a server name or alias — use `_`.
- **`mcp list` is filtered by the calling key.** The proxy returns only the
  servers the key may reach (a key with no MCP grant sees none);
  `mcp groups` lists every group. Run audits with an admin key. `mcp tools`
  is always "what THIS key sees" — use `mcp access` for other keys.
- **MCP secrets hide outside `credentials`.** An admin read still returns the
  server URL (integrations often embed the API key in the path or query),
  `static_headers`, `env`/`env_vars` and stdio `args`. Everything this CLI
  prints cuts URLs to their origin, masks header/env values and lists
  credential fields by name only. MCP credentials have no `os.environ/`
  indirection on the proxy, so `mcp add|update --auth-value-env VAR` reads the
  secret from the CLI's environment (never argv) and `--dry-run` masks it.
- **Key MCP grants merge per field.** `/key/update` merges `object_permission`
  field by field, so `keys update K --mcp-groups g` leaves the key's
  `mcp_servers`, vector stores and agents alone. Group names are checked
  against `/v1/mcp/access_groups` first (a typo would grant nothing,
  silently); server refs are resolved to ids. `keys rotate` copies the grant
  to the new key (not the old key's permission row id).
- **`mcp delete` leaves stale grants.** The proxy does not remove the server
  id from keys that named it; the output lists them (`keys_with_stale_grant`).
- **Sessions are per worker process.** `mcp sessions` / `sessions-close` act
  on whichever worker answered (`worker_pid`); with several workers or pods,
  repeat or target each node with `--url`. Both need an admin key (403
  otherwise). `mcp health` connects to every server; `mcp list` does not
  unless `--health`.

- **Guardrails have two sources and two lists.** `GET /guardrails/list`
  (plain `guardrails list`, and `drift`) is config.yaml ONLY; DB guardrails
  added through the UI or `guardrails add` show only in `guardrails list
  --all` (`/v2/guardrails/list`), `SRC=db`. Config guardrails are read-only
  through the API: `update`/`delete` refuse them.
- **Guardrail updates are PATCH, never PUT.** Every read is masked, so a
  read-modify-PUT would write the masked `api_key` back. PATCH can change the
  name, `default_on`, other params and the description; `mode` and the
  integration cannot be changed in place (delete and re-add). `default_on`
  lives inside `litellm_params` — the proxy's own docstring shows it
  top-level, where it is silently ignored. Renaming a guardrail breaks keys
  and requests that reference the old name.
- **Guardrail secrets.** The proxy's mask keeps four real characters; this CLI
  masks secret-named values completely and cuts `api_base` to its origin.
  `--api-key` and secret-named `--param`s only take `os.environ/NAME` (the
  proxy resolves it when it loads the guardrail), so no key enters the DB or
  shell history.
- **`guardrails test` is a real request.** It runs the guardrail on your text
  through `/guardrails/apply_guardrail` (logged, possibly billed by the
  guardrail service). A 4xx other than 401/403/404 is the guardrail blocking:
  verdict `blocked`, exit 3. `guardrails logs` leaves prompt/response snippets
  out unless `--snippets` — they are user content.
- **`mcp access` default-deny.** A key with no MCP grant, no team, no
  organisation and no admin owner gets 403 "The key has no MCP servers
  granted" from the gateway, so it shows `(none)` (it still reaches
  `allow_all_keys` servers). Owner roles come from the user list; if that
  cannot be read, no admin role is assumed.

- **Fallback lists have an owner.** A list config.yaml sets (`SRC=config`)
  is owned by the config: the proxy ignores the DB copy, so `POST /fallback`
  would change only the worker that answered, `/router/settings` would keep
  showing the config value, and a restart drops it. `fallbacks set|delete`
  refuse those; edit the config. Lists the config does not set are DB-owned
  (needs `STORE_MODEL_IN_DB`) and survive restarts, but are not in git:
  `drift` reports each rule as `fallbacks.<model>` until the config says the
  same. Chains are compared rule by rule; order inside a chain matters.
- **`/fallback/{model}` cannot address a name with `/`.** The route's path
  parameter does not match a slash, escaped (`%2F`) or not: the proxy answers
  404 `{"detail": "Not Found"}` for `ollama-cloud/glm-5.2` and similar names.
  `fallbacks show` and `fallbacks test` therefore resolve the chain locally
  from `/router/settings` for such names, and on any 404 (`resolved_by:
  router_settings`). A `stripped` or `provider_prefixed` `match` carries a
  `note`: the proxy applies it only if the prefix is a LiteLLM provider.
  `fallbacks delete` refuses such names (nothing is sent). Remove the rule
  by rewriting the DB list (Admin UI router settings or `POST
  /config/update`), or in config.yaml if the config owns it. `fallbacks set`
  is unaffected: the name goes in the body.
- **`fallbacks test` costs one request.** It sends a chat completion with
  `mock_testing_fallbacks` (or the context-window / content-policy variant),
  so the primary is failed without touching it and a fallback answers. The
  serving group comes from `x-litellm-model-id`. Chat groups only.
- **Paused, not deleted.** `models block` sets the DB row's `blocked` flag:
  the router skips the deployment, every setting stays, `models unblock`
  restores it. The proxy pauses DB deployments only, and the CLI takes one
  deployment id at a time (a group name is refused — pausing every replica is
  an outage). The reply is the DB row with `litellm_params`; it is never echoed.
- **Recorded health is a transition log.** Background health checks write a
  row only when a deployment's status changes; manual `/health` calls write
  one each time. So `health latest`'s SINCE is when the status began, and rows
  of deleted deployments linger (`gone`). Error text is upstream exception
  text: URLs are cut to their origin and key-shaped tokens masked.
- **`cache ping` is a write, and every /cache/* route is proxy-admin.** The
  ping PINGs the cache AND stores a test entry (`async_add_cache`), which is
  how it proves writes work — treat it as a health probe, not a pure read.
  A key that is not proxy admin gets 401 "Only proxy admin can be used…"
  on every cache route (seen live). `cache info` failures all surface as
  503 "Service Unhealthy (…)" — including "no cache" and "not redis", because
  the handler wraps its own errors.
- **Cache settings have TWO owners, the reverse of fallbacks.** `GET
  /cache/settings` shows the DB row overlaid with REDIS_* env — it CANNOT see
  a cache configured in config.yaml. Once a DB row exists it beats
  config.yaml's `cache_settings` at every restart (`init_cache_settings_in_db`),
  so `cache settings update` over empty reported settings (no row, no env)
  needs `--yes`: it pins a row that will shadow the config's cache. `drift`
  cannot see the DB row either.
- **The settings view can name the WRONG Redis.** When config.yaml points the
  cache at one Redis and the REDIS_* env names another (the usage /
  rate-limit store, LiteLLM's `coordination_redis`), `GET /cache/settings`
  reports the env's Redis, not the live cache (seen on the house proxy:
  settings said litellm-redis, `cache ping` said litellm-cache). Saving that
  would move cached answers onto the usage store, where LRU eviction pushes
  out rate-limit and budget counters. `cache settings` warns, and `cache
  settings update` refuses unless `--set host=…` names the target or `--yes`
  is passed; both ping the live cache to compare.
- **`cache settings update` REPLACES the map — the CLI merges for you.** The
  endpoint keeps only credentials that are omitted or echoed as
  `***REDACTED***`; every other field not re-sent is lost. The CLI therefore
  sends the effective settings with your `--set` changes applied (markers
  ride along; the proxy maps them back to the stored secrets). A stored
  credential belongs to its connection target: change host/port/url/nodes
  without a new `--set password=os.environ/NAME` and the proxy DROPS the
  stored one (anti-replay) — the CLI names what will drop. Literal secrets
  and userinfo-bearing urls are refused; pass `os.environ/NAME` (the redis
  client resolves it at connect time). `redis_type` is UI-only (derived from
  the node lists) and refused on `--set`.
- **Cache invalidation keys come from response headers.** Every proxied
  response carries `x-litellm-cache-key`; that is the string `cache delete`
  takes. Deleting a key means the next request for that prompt re-hits (and
  re-pays) the backend; `cache flushall` does that for every cached response.
  Redis-backed caches only.
- **Wide windows do not download.** `spend --days 90` and `logs summary` pull
  every raw row in the window and aggregate client-side — slow and heavy on a
  busy proxy. `usage report` asks the proxy's own aggregation
  (`/global/spend/report`, the numbers behind the Admin UI's charts) instead:
  per day/team/key/model/customer, `--start/--end` for calendar windows.
  There is no per-request drill-down from there — narrow the window and use
  `logs list`. The report's `api_key` rows are sha256 hashes, named the same
  way `spend --by key` names callers; `--by day` is summed client-side (the
  endpoint has no day-only form), so it is still a single small response.

## Site policy files

`lint --help-policy` prints the format. A policy turns decisions an estate has
recorded (ADRs, runbooks) into checks, so an agent tuning the proxy cannot
quietly undo one: routing strategy, required pre-call checks, required
guardrails, advertised context per deployment, secrets from env. Keep the policy
file next to the config it governs, in the same repo.
