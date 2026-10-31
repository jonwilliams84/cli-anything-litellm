# cli-anything-litellm

Administer, tune and audit a [LiteLLM](https://github.com/BerriAI/litellm) proxy from the shell.

- **Inspect:** status, per-deployment health (live, or the proxy's own recorded checks — `health latest` / `health history`, no backend called), model groups and replicas, live router settings, guardrails, recent requests (per-request logs: who called what, tokens, spend, latency, cache — filterable by key, user, team, model and tag).
- **Manage:** virtual keys (generate, update limits, block, rotate, delete — tagged for chargeback), user accounts (create with role/team/budget, update, delete), teams (create with members and budget, update, member add/remove, block, delete), budgets (reusable spend limits keys reference by `--budget-id`: create, update, delete — change the cap once instead of re-pricing each key), customers (end users of your app: track and cap by `user_id` — list, create, update, block, delete), DB-only model deployments (`models add` / `models delete` — what the Admin UI does), spend, spend-by-tag.
- **Routing resilience:** fallback chains (list, show the effective chain — resolved from router settings for group names with `/`, which the proxy's `/fallback/{model}` route cannot match — set, delete, and `fallbacks test` — fail a group on purpose and see which fallback served), and pausing one DB deployment without deleting it (`models block` / `models unblock`). Fallback lists config.yaml owns are refused (the proxy would drop the change on restart).
- **MCP gateway:** the MCP servers the proxy fronts — list, health, tools, access groups, add/update/delete DB servers, live gateway sessions — and which keys reach which servers (`mcp access`, `keys generate|update --mcp-groups/--mcp-servers`). Secrets embedded in server URLs, headers and env are never printed.
- **Response cache:** the Redis (or local) cache that serves repeated prompts without hitting the backend — `cache ping` (alive? what is it? — also writes a test entry, exit 2 down), `cache info` (memory, hit rate, evictions, keyspace of the Redis under it), `cache settings` (the DB row + REDIS_* env the proxy reports, credentials redacted), `cache settings test` (try new credentials without saving), `cache settings update` (merge changes over the effective settings — warns when a connection-target change makes the proxy drop the stored credential, and refuses to carry over a target that isn't the live cache's), `cache delete` (invalidate the keys a response's `x-litellm-cache-key` header names) and `cache flushall` (drop everything). Secrets ride as `os.environ/NAME` refs, never argv.
- **Guardrails:** config and DB guardrails side by side, add/update/delete DB guardrails (PATCH only, secrets as `os.environ/` refs), `guardrails test` a text against one, usage and fail rates per guardrail, the requests a guardrail judged, and the team submission queue.
- **Report (server-side):** `usage report` reads `/global/spend/report`, the proxy's own DB aggregation behind the Admin UI's charts — spend and token totals per day, team, key, model or end user over a window you can make as wide as a quarter (`--start/--end`), without downloading the log.
- **Tag management:** tags are the chargeback dimension (`keys generate --tags`, `spend --by tag`), and the proxy puts them in a *table* too (`GET /tag/list`, `POST /tag/update`, `POST /tag/delete`): a tag object carrying a description, the keys riding it, the models it is allowed on and a `budget_id` — a budget the proxy applies to every request *carrying the tag*, across all keys that share it. `tags update` is an upsert (there is no `/tag/create` — a name not in the table gets its row created) and merges over the row's current fields like `keys update`; `tags delete` strips the tag from the keys and teams riding it and deletes the row.
- **Audit:**
  - `drift` compares the config.yaml in git with the running proxy, including models that exist only in its database and fallback rules set through the API.
  - `fleet diff` compares proxies behind one VIP.
  - `lint` checks a config against built-in hygiene rules and a site policy file.

Read-only by default. Mutations support `--dry-run`, and destructive ones need `--yes` off a TTY. Every command takes `--json`.

```bash
pip install -e .
export LITELLM_URL=https://litellm.example LITELLM_API_KEY=sk-...
cli-anything-litellm config test
cli-anything-litellm models list
cli-anything-litellm models add --name qwen --model hosted_vllm/qwen --api-base http://n1:8000/v1 --api-key os.environ/QWEN_KEY
cli-anything-litellm teams create --alias eng --team-id team-eng --models qwen --max-budget 50 --member svc@corp.io
cli-anything-litellm users create --user-id svc-bot --email svc@corp.io --teams eng --max-budget 5
cli-anything-litellm budgets create --budget-id eng-2026 --max-budget 100 --duration 30d --model-max-budget '{"gpt-4o": 0.01}'
cli-anything-litellm keys generate --alias svc --user svc-bot --team team-eng --rpm 60 --budget-id eng-2026
cli-anything-litellm keys generate --alias feat --user svc-bot --team team-eng --tags feat-checkout,team-eng
cli-anything-litellm keys update feat --tags feat-checkout   # re-tag a key; other metadata is kept
cli-anything-litellm spend --by tag               # the chargeback view: bill per tag (one row per tag)
cli-anything-litellm logs list --tag feat-checkout --limit 20   # the requests behind one tag's spend
cli-anything-litellm customers create --user-id cust-x --alias acme --budget-id eng-2026
cli-anything-litellm customers list --budget eng-2026          # end users, capped by the shared budget
cli-anything-litellm customers block acme --yes                # their user_id stops working, reversibly
cli-anything-litellm budgets info eng-2026   # the budget and every key/team on it
cli-anything-litellm budgets update eng-2026 --max-budget 200   # re-prices every attached key
cli-anything-litellm logs list --key svc --limit 50   # the requests behind `spend` totals
cli-anything-litellm logs summary --team eng   # traffic + latency p50/p95 over the window
cli-anything-litellm usage report --by day --days 90    # the wide picture, aggregated by the proxy's DB
cli-anything-litellm usage report --by api_key --start 2026-01-01 --end 2026-01-31  # a monthly spend report
cli-anything-litellm tags list                              # the tag table (config-declared tags: bare names)
cli-anything-litellm tags update feat-checkout --budget-id eng-2026   # one cap for every request carrying the tag
cli-anything-litellm tags delete feat-checkout --yes        # untagged keys and teams keep working
cli-anything-litellm mcp list --health                      # MCP servers behind the gateway (SRC=config|db) and their status
cli-anything-litellm mcp health                             # exit 2 when an MCP server is down
cli-anything-litellm mcp access --server grafana            # which keys lose those tools if it is
cli-anything-litellm keys update rufus --mcp-groups devtools,home   # grant MCP access groups (merged, other grants kept)
HA_MCP_TOKEN=... cli-anything-litellm mcp add --name home_assistant --alias ha --url https://ha:8123/api/mcp \
  --access-groups home --auth-type bearer_token --auth-value-env HA_MCP_TOKEN   # secret from env, never argv
cli-anything-litellm mcp sessions                           # live gateway sessions on the worker that answered
cli-anything-litellm guardrails list --all                  # config + DB guardrails (SRC=config|db)
echo 'my card is 4111 1111 1111 1111' | cli-anything-litellm guardrails test pii-mask   # passed / modified / blocked (exit 3)
cli-anything-litellm guardrails usage --days 30             # evaluated, blocked, fail rate; exit 2 if one is critical
cli-anything-litellm guardrails logs prompt-injection --action blocked
cli-anything-litellm guardrails add --name pii-mask --guardrail presidio --mode pre_call,post_call \
  --api-base http://presidio:3000 --api-key os.environ/PRESIDIO_KEY --dry-run
cli-anything-litellm health latest                          # last recorded status per deployment; exit 2 if one is down (no backend called)
cli-anything-litellm health history --model qwen --status unhealthy   # when did it go down?
cli-anything-litellm models block qwen-b --yes              # pause one DB replica (says how many keep serving)
cli-anything-litellm fallbacks set qwen --to gpt-4o,claude  # checked against live groups; refused if config.yaml owns the list
cli-anything-litellm fallbacks test qwen                    # fail qwen on purpose: which fallback served? (one real completion)
cli-anything-litellm models unblock qwen-b                  # back into rotation
cli-anything-litellm cache ping                              # alive? what cache? (writes a test entry; exit 2 if down)
cli-anything-litellm cache info                              # the Redis under it: memory, hit rate, evictions, keyspace
cli-anything-litellm cache settings                          # the stored settings (DB row + REDIS_* env; credentials redacted)
cli-anything-litellm cache settings test --set host=r2 --set password=os.environ/R2_PASS   # try, never save
cli-anything-litellm cache settings update --set ttl=300     # merge over the effective settings; --yes pins a row over config.yaml's cache
cli-anything-litellm cache delete litellm_proxy:abc --yes   # invalidate one stale response (its x-litellm-cache-key)
cli-anything-litellm cache flushall --yes                    # every cached response gone; backends serve the next requests
cli-anything-litellm drift --config litellm/config.yaml
cli-anything-litellm lint --config litellm/config.yaml --policy litellm/policy.yaml
cli-anything-litellm fleet diff --node https://gw-1:4000 --node https://gw-2:4000
```

See [`skills/cli-anything-litellm/SKILL.md`](skills/cli-anything-litellm/SKILL.md) for the command map and the proxy behaviours it accounts for.

Tested against LiteLLM **v1.100.1**; the MCP (v0.10.0), guardrail (v0.11.0), routing-resilience (v0.12.0), response-cache (v0.13.0) and tag-management (v0.14.0) commands against **v1.106.3**. See [`TEST.md`](TEST.md) for the test layout and results.
