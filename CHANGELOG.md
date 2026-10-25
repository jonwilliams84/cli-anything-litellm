# Changelog

## [0.10.0] — 2026-10-25

The MCP gateway: the proxy's other half, from the shell.

A LiteLLM proxy fronts MCP servers the way it fronts model deployments: one
MCP endpoint, every server's tools behind it as `<server>-<tool>`, access
decided per key by `object_permission` (`mcp_access_groups`,
`mcp_servers`). The CLI covered models, keys, spend and drift but not one
`/v1/mcp/*` route, so "is the grafana MCP server up", "which keys lose those
tools if it is not", "grant this key the home group" and "add a server"
meant the Admin UI or hand-written curl. Now (shapes from v1.106.3,
`litellm/proxy/management_endpoints/mcp_management_endpoints.py`):

- **`mcp list [--health]`** — every server: name (the tool prefix), source
  (`config` from config.yaml, read-only through the API, or `db`),
  transport, auth type, URL origin, access groups, health on request.
- **`mcp health [SERVER...]`** — `GET /v1/mcp/server/health`; exit 2 when
  any server is unhealthy, unhealthy ones listed first.
- **`mcp info <id|alias|name>`** — the full record (the proxy health-checks
  it live), redacted.
- **`mcp tools [--server S]`** — the tools the calling key sees, attributed
  to servers, with the servers' read-only/destructive hints.
- **`mcp groups`** — each access group and its servers, plus servers open to
  every key (`allow_all_keys`).
- **`mcp access [--server S] [--team T]`** — which keys reach which servers
  through their own grant (groups ∪ named servers ∪ `allow_all_keys`); keys
  with no grant of their own are shown as inherited, not guessed.
- **`mcp add` / `mcp update` / `mcp delete`** — DB servers via
  `POST|PUT /v1/mcp/server` and `DELETE /v1/mcp/server/{id}`. `add` defaults
  `--transport http` (the proxy defaults to `sse`), refuses stdio (it runs a
  process on the proxy host) and names with `-` (the tool separator) before
  the proxy does. `update` sends only the flags given: the endpoint is a
  partial update where a null clears a field. Config servers are refused
  with a pointer to the config. `delete` is `--yes`-gated and names the keys
  still holding an explicit grant to the deleted id (the proxy leaves them).
- **`mcp sessions` / `mcp sessions-close --user U|--prefix P`** — live
  stateful gateway sessions, per worker process; closing is `--yes`-gated.
- **`keys generate|update --mcp-groups g1,g2 --mcp-servers s1`** — set a
  key's MCP grant (`none` = the proxy's opt-out sentinel). Groups are
  checked against `/v1/mcp/access_groups` and server refs resolved to ids
  before the proxy is called. The proxy merges `object_permission` per field,
  so the key's other grants survive. `keys list --json` rows gain `mcp`.

### Security
- MCP server records carry upstream secrets outside `credentials`: in the
  URL (path or query), `static_headers`, `env`/`env_vars` and stdio `args`.
  Every MCP output cuts URLs to their origin, masks header/env values, and
  lists credential fields by name only. MCP credentials have no
  `os.environ/` indirection on the proxy, so `--auth-value-env VAR` reads the
  secret from the CLI's environment rather than argv, and `--dry-run`
  prints it masked.

### Fixed
- `keys rotate` dropped the key's `object_permission` — its MCP servers and
  access groups, vector stores and agents — so the replacement key silently
  lost every one of those grants. It now copies the grant (not the old key's
  permission row id, which would make the two keys share one row).
- `spend`, `logs list` and `logs summary` now page through `/spend/logs/v2` (100 rows a page,
  without prompt and response columns). The unpaged `/spend/logs?summarize=false` loaded every
  row, prompts included, into the proxy: a 3-day window OOM-killed both pods of a 2Gi v1.106.3
  proxy. It also truncates silently to its newest rows, so long-window totals could be short.
  It remains only as the fallback when a proxy has no v2 route (404).
- `spend --by key` and `logs` named the master key's own calls `(unknown litellm_pr…)`.
  Newer proxies (seen on v1.106.3) log them under the literal `litellm_proxy_master_key`
  instead of the key's sha256, so both names now map to `(master key)`.
- Log timestamps were off by the local UTC offset outside UTC (an hour on BST). The proxy
  writes `startTime` as naive UTC, which `astimezone()` read as local time.

Core: `core/mcp.py`; `core/backend.py` gains `put` and `delete`. Tests:
`tests/test_mcp.py` (69: core units, CLI over the mocked proxy, two
workflows, the installed command via `_resolve_cli`) with fixtures in `tests/mcp_fixtures.py`, each citing the
v1.106.3 function whose response it reproduces (the server row also checked
against a live v1.106.3 proxy). Docs: README, `TEST.md`, `CLAUDE.md` and the
SKILL (both copies). (v0.10.0: 262 tests, 90.3% coverage.)

## [0.9.0] — 2026-10-24

Server-side spend reports: the wide-window money view the CLI could not carry.

Until now every spend number came from the same trick — download the raw
`/spend/logs` rows in the window and aggregate them client-side (`spend`,
`logs summary`). Exact and shape-tolerant, but a 90-day window on a busy
proxy means moving thousands of rows to produce a few numbers, and there was
no per-day picture at all. The proxy has always kept that log in its database
and aggregated it server-side — the numbers behind the Admin UI's usage
charts — and ships an endpoint for reading them: `/global/spend/report`
with `group_by=team|api_key|model|customer`. The CLI never exposed it. Now:

- **`usage report [--by day|team|api_key|model|customer] [--days N]`**
  (default `--by day`) — spend and token totals per day (or per
  team / key / model / end user) over the window, one small response
  instead of a log download.
- **`--start YYYY-MM-DD --end YYYY-MM-DD`** — an explicit window for reports
  wider than any sensible log download (a month, a quarter). One-sided or
  inverted ranges and non-dates are refused before the proxy is called.
- **`--limit N`** caps the newest rows shown; the roll-up totals in the
  header and in `--json` come from the full report.

Trade-offs are documented, not hidden: there is no per-request drill-down
from the report (narrow the window and use `logs list`), and the report's
group ids are the proxy's own — an `api_key` row is the log's sha256(key)
hash, named exactly the way `spend --by key` names callers (through the live
key list, the alias recorded at request time, or `(master key)` — the hash
itself never reaches the output), and a row that names no group lands in
`(unattributed)` rather than being dropped. `--by day` has no server-side
form (the proxy always groups by an entity within the day), so it sums the
default rows client-side — still one request.

Core: `core/usage.py` (`window`, `params`, `as_rows`, `normalize`, `totals`),
unit-tested in `tests/test_core.py` (windows, dimension mapping, envelope
tolerance, hash naming, day summation, roll-ups); E2E over the mocked proxy
in `tests/test_full_e2e.py` (query construction, explicit ranges validated
client-side, `--json` shape, `--limit`, the no-secret-in-output rule) and the
workflow test proving `spend --by key` and `usage report --by api_key` name
the same caller the same way and agree on the number, with `logs list --key`
as the drill-down. Docs: README, `TEST.md` and the SKILL (both copies).
(v0.9.0: 190 tests, 87.9% coverage.)

## [0.8.0] — 2026-10-18

Tag-based spend attribution: the chargeback dimension between keys and models.

A *tag* is a free label on a virtual key — a feature (`feat-checkout`), a
project, a cost centre. The proxy copies the key's `metadata.tags` into its
spend log, where every row's `request_tags` records what the request carried,
so one key can serve several features and still be billed per feature —
"how much does feature X cost" is answerable without minting a key per
feature. The CLI already read `tags` and `metadata` out of `keys rotate`
(the rotation carries them over to the replacement key) but had no way to set
them and nowhere the numbers appeared. The tag story is now complete across
the key lifecycle and the two readers of the spend log:

- **`keys generate --tags feat-x,team-a`** — tags a new key (stored as
  `metadata.tags`). A value that parses to nothing (`" , ,"`) is refused
  before the proxy is called.
- **`keys update <key> --tags …`** — re-tags a live key: the tag set is
  replaced, but merged over the key's existing metadata, because
  `/key/update` swaps the whole metadata object — a plain retag must not
  silently drop the other metadata fields.
- **`keys list`** — a TAGS column (and a `tags` field in `--json`),
  tolerant of the flat `tags` older proxies emit next to `metadata.tags`.
- **`spend --by tag`** — the money view per tag, aggregated client-side from
  `/spend/logs` exactly like the other `spend` groupings. Unlike those, a
  request can ride *several* tags at once: it is counted under each of them,
  and rows without tags are counted once, as `(untagged)`.
- **`logs list --tag T` / `logs summary --tag T`** — the drill-down behind one
  tag's number, filtered client-side (a request names every tag it carried);
  `logs list` rows now also carry the row's tags (`tags`, list or null) and
  the filter shows in the header.
- Core: `core/tags.py` (`parse_tags`, `as_metadata`, `key_tags`,
  `log_row_tags`, `filter_by_tags`, `spend_by_tag`), unit-tested in
  `tests/test_core.py`; E2E (dry-run metadata, empty-tag refusal, metadata
  merge on update, both key shapes in `keys list`, the `spend --by tag`
  aggregation and `logs --tag` filters) and the tag-chargeback workflow
  (generate → spend → logs drill-down → retag) in `tests/test_full_e2e.py`.
- Docs: README, `TEST.md` and the SKILL (both copies) cover the new commands
  and the JSON-text `request_tags` column quirk they pair with.
  (v0.8.0: 170 tests, 87.1% coverage.)

## [0.7.0] — 2026-10-11

Request logs: per-request observability from the spend log the CLI already
read for `spend` totals — the drill-down from "this key spent $9" to *which
requests* and *how slow*.

A LiteLLM proxy records every completion it serves in its spend log:
`request_id`, call type, model group, the (hashed) key that paid, the end
user and team, token counts, spend, cache hit, and timestamps. Until now the
CLI reduced all of that to aggregate totals per entity — there was no way to
see an individual request, whether a latency tail was forming, or what a
given key actually did. The new `logs` group exposes those rows directly:

- **`logs list`** — GET `/spend/logs` over the same `--days` window `spend`
  uses (default 7; the log is written asynchronously, so fresh traffic shows
  up late), one row per request, newest first (`--limit`, default 25):
  timestamp, model, caller, user, call type, tokens, spend, latency, cache
  hit. Filters: `--key` (an `sk-…` key is hashed before matching — the log
  stores sha256, never the secret), a hashed token, or an alias resolved
  against `keys list`; plus `--user`, `--team` and `--model`. The key/user/
  team/model filters are applied client-side so they survive proxies that
  ignore the query params, and unknown or ambiguous alias references are
  refused before the proxy is called. The output names callers without ever
  echoing the hash: from the live key list (`(master key)` for the key we
  authenticate with) or the alias recorded in the row at request time
  (`svc (gone)` for a key renamed, rotated or deleted since).
- **`logs summary`** — the traffic and latency view over the same filtered
  rows, next to `spend`'s money view: request count, spend, token totals,
  cache hits, latency p50/p95/max and a per-model breakdown sorted by spend,
  each model with its own latency percentiles.

The log-row shapes vary across proxy versions, so `core/logs.py` is
tolerant: `startTime`/`start_time` (ISO-8601 or epoch seconds), latency from
`endTime - startTime` with the proxy's `ending_latency_s` as the fallback for
failed rows (no `endTime`), and the model group from `model_group` `or`
`model`.

- Core: `core/logs.py` (`normalize`, `filter_logs`, `token_of`,
  `key_alias_map`, `latency_ms`, `summary`), unit-tested in
  `tests/test_core.py`; `logs list`/`logs summary` E2E (caller naming,
  each filter with `--limit`, ambiguous-alias error, master/gone callers) and
  the `spend --by key` → `logs list --key` drill-down workflow in
  `tests/test_full_e2e.py`. `spend` now fetches its window through the same
  helper — no behaviour change.
- Docs: README, `TEST.md` and the SKILL (both copies) cover the new commands
  and the hashing/aliasing quirk they pair with. (v0.7.0: 154 tests, 85.5%
  coverage.)

## [0.6.0] — 2026-10-03

Customer (end-user) management: spend control gets its sixth leg, next to keys,
teams, users, budgets and spend.

A *customer* in LiteLLM is an end user of the app behind the proxy — the
`user_id` the app passes with every request. The proxy attributes spend to that
id live, caps it with `max_budget` (inline or a shared `budget_id`) and
`tpm`/`rpm` limits, and can fail every request that names it with one
`blocked` switch — charge one app user without minting them a key. Until now
those customers could only be created or changed in the Admin UI, and the CLI
could not even list them. The new `customers` group covers the whole
`/customer/*` family:

- **`customers list`** — GET `/customer/list`, following the proxy's
  pagination when it comes as `{"customers": [...], "total_pages": ...}` and
  tolerating the bare-list shape older proxies return; `--budget F` filters to
  customers on one shared cap. One row per end user: alias, spend, budget,
  caps, blocked.
- **`customers info <user_id|alias>`** — GET `/customer/info`. Keyed by
  `user_id` alone, so a unique alias is resolved against the live list first.
- **`customers create`** — POST `/customer/new` with `--user-id` (what the app
  passes with each request), `--alias`, `--max-budget`, `--budget-id` (a
  `budgets create` cap applies to end users too), `--rpm` and `--tpm`. A
  customer with neither a user id nor an alias is refused client-side: the
  proxy would generate an id nobody can find later, the way an implicitly
  minted key without an alias hides in audits. `--dry-run` prints the exact body.
- **`customers update <user_id|alias>`** — POST `/customer/update`; given
  flags replace the field, omitted ones stay as-is. The ref is resolved against
  the live list first (unknown → error pointing at `customers list`), and a
  `--budget-id` here makes the shared cap override an inline `max_budget`.
- **`customers block` / `customers unblock <user_id|alias>`** — POST
  `/customer/block` and `/customer/unblock` with `{"user_ids": [id]}`. Blocking
  fails every request that names the end user's `user_id`, reversibly; the
  block needs `--yes` off a TTY.
- **`customers delete <user_id|alias>`** — POST `/customer/delete` with
  `{"user_ids": [id]}` — the family's write endpoints all speak plural, unlike
  the budget family where delete alone says `id`. Destructive: needs `--yes`
  off a TTY. Deleting a customer stops spend attribution for that end user; it
  deletes no key.
- Core: `core/customers.py` (`as_rows`, `normalize`, `new_customer`,
  `update_customer`, `ids_body`, `resolve_customer_target`), unit-tested in
  `tests/test_core.py`; E2E and a
  create→budget→block→unblock→delete workflow in `tests/test_full_e2e.py`.
- Docs: README, `TEST.md` and the SKILL (both copies) cover the new commands
  and the plural-deletion quirk they pair with.

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
