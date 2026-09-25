# cli-anything-litellm

Administer, tune and audit a [LiteLLM](https://github.com/BerriAI/litellm) proxy from the shell.

- **Inspect:** status, per-deployment health, model groups and replicas, live router settings, guardrails.
- **Manage:** virtual keys (generate, update limits, block, rotate, delete), teams, spend.
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
cli-anything-litellm drift --config litellm/config.yaml
cli-anything-litellm lint --config litellm/config.yaml --policy litellm/policy.yaml
cli-anything-litellm fleet diff --node https://gw-1:4000 --node https://gw-2:4000
```

See [`skills/cli-anything-litellm/SKILL.md`](skills/cli-anything-litellm/SKILL.md) for the command map and the proxy behaviours it accounts for.

Tested against LiteLLM **v1.100.1**.
