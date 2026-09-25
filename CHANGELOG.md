# Changelog

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
