# cli-anything-litellm

Administer, tune and audit a [LiteLLM](https://github.com/BerriAI/litellm) proxy from the shell.

- **Inspect:** status, per-deployment health, model groups and replicas, live router settings, guardrails.
- **Manage:** virtual keys (generate, update limits, block, rotate, delete), user accounts (create with role/team/budget, update, delete), teams (create with members and budget, update, member add/remove, block, delete), budgets (reusable spend limits keys reference by `--budget-id`: create, update, delete — change the cap once instead of re-pricing each key), customers (end users of your app: track and cap by `user_id` — list, create, update, block, delete), DB-only model deployments (`models add` / `models delete` — what the Admin UI does), spend.
- **Audit:**
  - `drift` compares the config.yaml in git with the running proxy, including models that exist only in its database.
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
cli-anything-litellm customers create --user-id cust-x --alias acme --budget-id eng-2026
cli-anything-litellm customers list --budget eng-2026          # end users, capped by the shared budget
cli-anything-litellm customers block acme --yes                # their user_id stops working, reversibly
cli-anything-litellm budgets info eng-2026   # the budget and every key/team on it
cli-anything-litellm budgets update eng-2026 --max-budget 200   # re-prices every attached key
cli-anything-litellm drift --config litellm/config.yaml
cli-anything-litellm lint --config litellm/config.yaml --policy litellm/policy.yaml
cli-anything-litellm fleet diff --node https://gw-1:4000 --node https://gw-2:4000
```

See [`skills/cli-anything-litellm/SKILL.md`](skills/cli-anything-litellm/SKILL.md) for the command map and the proxy behaviours it accounts for.

Tested against LiteLLM **v1.100.1**. See [`TEST.md`](TEST.md) for the test layout and results.
