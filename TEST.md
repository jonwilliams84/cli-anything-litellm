# Test documentation

All tests are offline: `pip install -e '.[dev]' && pytest`. No proxy is needed —
`be.get` / `be.post` are mocked, and the fixtures reproduce the response shapes
of a real LiteLLM v1.100.1 proxy (the MCP and guardrail fixtures, `tests/mcp_fixtures.py` and `tests/guardrail_fixtures.py`:
v1.106.3, each factory citing the source function it reproduces).

## Layout

| File | Scope |
|---|---|
| `tests/test_harness.py` | Original build: core (`models`, `drift`, `lint`, `backend`) and CLI behaviours (dry-run, confirm, rotation, spend grouping, 401 hint). |
| `tests/test_core.py` | Unit tests for the newer cores: `models.new_deployment` / `models.resolve_model_target` (body construction, required args, id vs model-name match), `users.new_user` / `users.update_user` / `users.resolve_user_target` / `users.normalize` (body + role validation, id vs unique-email resolution, ambiguous email refused), `teams.new_team` / `teams.update_team` / `teams.resolve_team_target` / `teams.normalize` / `teams.member_entry` (body + member-role validation, id vs unique-alias resolution, ambiguous alias refused), `budgets.as_rows` / `budgets.new_budget` / `budgets.update_budget` / `budgets.normalize` / `budgets.resolve_budget_target` (envelope tolerance, nothing-to-cap refusal, per-model caps via dict or JSON string, exact-id match) and `customers.as_rows` / `customers.new_customer` / `customers.update_customer` / `customers.normalize` / `customers.ids_body` / `customers.resolve_customer_target` (envelope tolerance, user_id-or-alias requirement, plural delete body, id vs unique-alias resolution, ambiguous alias refused), and `logs.latency_ms` / `logs.filter_logs` / `logs.normalize` / `logs.token_of` / `logs.key_alias_map` / `logs.summary` (shape tolerance, sha256 vs alias vs ambiguous-alias key references, per-field filters, percentiles, per-model breakdown), and `tags.parse_tags` / `tags.as_metadata` / `tags.key_tags` / `tags.log_row_tags` / `tags.filter_by_tags` / `tags.spend_by_tag` (tag parsing and emptiness refusal, metadata merge, `metadata.tags` vs flat `tags` vs the JSON-text `request_tags` column, per-row tag filters, the several-tags-per-request aggregation with `(untagged)`), and `usage.window` / `usage.params` / `usage.as_rows` / `usage.normalize` / `usage.totals` (window construction and validation, dimension→`group_by` mapping, envelope tolerance, sha256-named key rows with the recorded alias winning, the `day` client-side summation, roll-ups). |
| `tests/test_full_e2e.py` | End-to-end and workflow tests: `models add` / `models delete` over the mocked API (`--dry-run` body == exact request body, `--yes` gating, replica counting, unknown-ref error), a `drift`-gap → `models add` → `models list --deployments` workflow, the delete-what-`drift`-found workflow, the user-management set (`users list/info/create/update/delete` + the onboard→key→delete workflow), the team-management set (`teams create/update/delete/member-add/member-delete/block/unblock` + the team→key→delete workflow), the budget-management set (`budgets list/info/create/update/delete` + the create→attach→reprice→delete workflow) and the customer-management set (`customers list/info/create/update/block/unblock/delete` + the onboard→budget→block→unblock→delete workflow), and the request-log set (`logs list` / `logs summary`: caller naming, key/user/team/model filters, `--limit`, ambiguous-alias error, latency percentiles) plus the `spend --by key` → `logs list --key` drill-down workflow, and the tag set (`keys generate --tags` / `keys update --tags` with metadata merge and empty-refusal, `keys list` tags column, `spend --by tag`, `logs list --tag` / `logs summary --tag`, and the tag-chargeback workflow: generate→spend→logs→retag), and the usage-report set (`usage report`: `--days` and explicit `--start/--end` query construction, range validation before the proxy, `--json` shape, `--by api_key` naming through the live key list with no hash in the output, `--limit`, the junk-row tolerance) plus the money-view workflow (`spend --by key` and `usage report --by api_key` name the same caller the same way and agree on the total; `logs list --key` is the drill-down). |
| `tests/test_mcp.py` | MCP gateway (v0.10.0): `core/mcp.py` units (envelope tolerance, labels, URL-origin redaction, server redaction never carrying a secret, row normalisation, id/alias/name resolution with ambiguity refused, POST/PUT body construction and every client-side refusal, env-sourced secrets, dry-run masking, tool→server attribution, key grants, effective reach, grant validation, stale grants, path escaping), `backend.put/delete`, the `mcp` command set over the mocked proxy, `keys generate|update --mcp-*`, `keys list` `mcp` field, `keys rotate` carrying the grant, two workflows, and subprocess tests of the installed command (`_resolve_cli`: help, a dry-run that never leaks the env secret, a loud failure on an unreachable proxy). |
| `tests/test_guardrails.py` | Guardrails (v0.11.0): `core/guardrails.py` units (location, redaction that also hides the proxy's 4-character partial mask, row normalisation, id/name resolution with ambiguity refused, mode parsing, `--param` typing and literal-secret refusal, POST and PATCH bodies and every refusal, apply verdicts, usage windows, overview/log/submission rows), `backend.patch`, the `guardrails` command set over the mocked proxy, and two workflows. |

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

## Request-log coverage (`logs ...`, v0.7.0: `core/logs.py`)

- `logs list` reads `/spend/logs` over the window `spend` uses (`--days`,
  default 7; the log is written asynchronously, so fresh traffic shows up
  late) and shows one row per request, newest first (`--limit`, default 25):
  timestamp, model group, caller, user, call type, tokens, spend, latency,
  cache hit. The row's `api_key` is the sha256 hash of the key — the output
  names callers through the live key list (`(master key)` for our own key)
  or the alias recorded in the row (`svc (gone)`), never the hash.
- `--key` accepts an `sk-…` key (hashed before filtering), a hashed token, or
  an alias resolved against `keys list` — an unknown or ambiguous alias is
  refused before the proxy is called. `--user`, `--team` and `--model` filter
  client-side (so they work even on proxies that ignore the query params),
  and the output carries `matched` (rows in the window that survive the
  filters) next to `shown`.
- `logs summary` aggregates the same filtered rows: request count, spend,
  token totals, cache hits, latency p50/p95/max and a per-model breakdown
  sorted by spend with each model's own latency percentiles — the traffic
  and latency view, next to `spend`'s money view.
- Core tolerates the shape variants proxies emit: `startTime`/`start_time`
  (ISO-8601 or epoch), the proxy's `ending_latency_s` fallback for rows with
  no usable `endTime` (failed calls), `model_group` `or` `model`, and hashes
  hidden in `metadata`.
- Workflow: `spend --by key` names a key burning budget → `logs list --key
  <alias>` names the requests behind that number (same log, drill-down view).

## Tag spend coverage (`tags`, v0.8.0: `core/tags.py`)

- A tag is a free label on a virtual key (LiteLLM's chargeback dimension
  between keys and models): the proxy copies the key's `metadata.tags` — set
  with `--tags` on `keys generate` / `keys update` — into the spend log, where
  each row's `request_tags` records what the request carried. `keys rotate`
  already carried tags and metadata over to the replacement key; now the rest
  of the lifecycle can set them (`keys list` shows a TAGS column, from
  `metadata.tags` or the flat `tags` older proxies emit).
- `spend --by tag` is the money view per tag, aggregated client-side from
  `/spend/logs` like every other `spend` grouping. Unlike those, a row can
  carry several tags at once: such a request is counted under *each* of its
  tags, and rows with no tags are counted once, as `(untagged)`. Sorted by
  spend.
- `logs list --tag T` / `logs summary --tag T` filter the request rows
  client-side (a request names every tag it carried) — the drill-down behind
  one tag's number. `logs list` rows carry the row's tags as `tags`
  (list or null); the human table is unchanged.
- Core tolerates the shape variants proxies emit: `request_tags` is a *text*
  column (a JSON-encoded list on current proxies, a bare list or
  comma-separated string on others, `null` on rows without tags — invalid
  JSON is still comma-split rather than dropped), and key tags come from
  `metadata.tags` or a flat `tags`.
- A `--tags` value that survives parsing with nothing usable (`" , ,"`) is
  refused before the proxy is called.
  `keys update --tags` merges over the key's existing metadata: `/key/update`
  replaces the whole object, otherwise a retag would silently drop the other
  metadata fields.
- Workflow: `keys generate --tags feat-x` → `spend --by tag` bills the
  feature → `logs list --tag feat-x` names the requests → `keys update --tags`
  moves later traffic onto the new label (dry-run shows the merged metadata).

## Usage report coverage (`usage report`, v0.9.0: `core/usage.py`)

- `usage report` reads `/global/spend/report` — the proxy's own aggregation of
  its spend log in the database (the numbers behind the Admin UI's charts) —
  so a wide money view is one small response instead of the raw-log download
  `spend` / `logs summary` perform.
- `--by day` (default) sums the report's entity rows client-side into one row
  per day (`spend`, token totals); `--by team|api_key|model|customer` maps
  straight to the endpoint's `group_by` and keeps one row per (day, entity).
- The window is `--days` (the same inclusive one `spend` uses, ending
  tomorrow) or an explicit `--start`/`--end` in YYYY-MM-DD for reports wider
  than any sensible log download (a month, a quarter). One-sided ranges, bad
  dates and inverted ranges are refused before the proxy is called.
- api_key rows are the log's sha256(key) hash — named exactly like
  `spend --by key`: through the live key list, an `api_key_alias` the proxy
  recorded at request time, `(master key)` for our own key, or
  `(unknown hh…)` for a hash nobody names; the hash itself never reaches the
  output. A row that names no group is reported as `(unattributed)` rather
  than dropped; number fields arrive as JSON strings or nulls and are coerced.
- `--limit N` caps the newest rows shown; the header/`--json` roll-up
  (`spend`, days, token totals) comes from the full report. There is no
  per-request drill-down from the report — narrow the window and use
  `logs list`.
- Workflow: `spend --by key` (raw log, client-side) and
  `usage report --by api_key` (server-side report) name the same caller the
  same way and agree on the total; `logs list --key <alias>` names the
  requests behind it.

## MCP gateway coverage (`mcp ...`, `keys --mcp-*`, v0.10.0: `core/mcp.py`)

Fixtures reproduce LiteLLM v1.106.3 (`tests/mcp_fixtures.py`):

| Fixture | Source (litellm/ in v1.106.3) |
|---|---|
| `mcp_server()` / `SERVERS` | `LiteLLM_MCPServerTable` (`models/mcp_server.py`) as `fetch_all_mcp_servers` returns it after `_redact_mcp_credentials` (`proxy/management_endpoints/mcp_management_endpoints.py`); config rows per `build_mcp_server_table` (`proxy/_experimental/mcp_server/mcp_server_manager.py`). Checked field-for-field against a live v1.106.3 `GET /v1/mcp/server`. |
| `health()` | `health_check_servers` — a bare `[{server_id, status}]` list. |
| `tool()` / `TOOLS` | `get_mcp_tools` — `{"tools": [Tool.model_dump(by_alias=True)]}`, names from `add_server_prefix_to_name` (`proxy/_experimental/mcp_server/utils.py`). |
| `GROUPS` | `get_mcp_access_groups` — `{"access_groups": sorted(...)}`. |
| `SESSIONS` / `session()` / `terminated()` | `MCPGatewaySessionsResponse`, `MCPGatewaySession`, `MCPGatewaySessionsTerminateResponse` (`types/mcp.py`), from `get_mcp_gateway_sessions_report` (`proxy/_experimental/mcp_server/server.py`). |
| `permission()` | `LiteLLM_ObjectPermissionTable` (`proxy/schema.prisma`) as `attach_object_permission_to_dict` (`proxy/management_helpers/object_permission_utils.py`) attaches it; matches a live v1.106.3 `/key/info`. |
| `key()` | `/key/list?return_full_object=true` rows from `_list_key_helper` (`proxy/management_endpoints/key_management_endpoints.py`), which attaches `object_permission`. |

- `mcp list` reads `/v1/mcp/server` and makes no server connection unless
  `--health` (then `/v1/mcp/server/health`); rows are `normalize` (name =
  alias or server_name, `source` config|db, URL origin only). No fixture
  secret (URL query, header, env var) reaches the output.
- `mcp info` resolves an alias/name to the id first and fetches
  `/v1/mcp/server/{id}`; output is `redact_server` (credential field names
  only). Unknown refs point at `mcp list`.
- `mcp health` exits 2 when any server is unhealthy and lists those first;
  named servers are sent as `server_ids`.
- `mcp tools` counts tools per server (longest label wins, so `ha_admin-x` is
  not filed under `ha`) and `--server` filters by id, alias or name.
- `mcp groups` lists group members and `allow_all_keys` servers; `mcp access`
  computes groups ∪ named servers ∪ open servers per key, shows keys with
  no grant as inherited only where a team, org or admin owner can grant (else `(none)`, since v0.11.0), honours the `no-mcp-servers` sentinel, filters by
  `--server`.
- `mcp add`: `--dry-run` prints the body with the credential masked (`**** (from
  $VAR)`) and sends nothing; the real POST carries the secret and the printed
  row does not. Refused before the proxy: `-` in name/alias, missing URL,
  stdio, reserved ids, unknown auth types, a credential without
  `--auth-type`, an unset `--auth-value-env`.
- `mcp update` PUTs only the flags given (`''` clears a list,
  `--no-allow-all-keys` sends `false`); nothing to change is an error. Config
  servers are refused by `update` and `delete`.
- `mcp delete` is dry-runnable, refuses without `--yes` off a TTY, and names
  the keys whose own grant still names the deleted server.
- `mcp sessions` / `sessions-close`: the selector is required and the prefix
  length checked client-side; dry-run, `--yes` gating, query params, and a
  clean error on the proxy's 403 for non-admin keys.
- `keys generate|update --mcp-groups/--mcp-servers` build `object_permission`
  (unknown group or server refused before the proxy; `none` → the opt-out
  sentinel; an MCP-only update is enough). `keys rotate` carries the grant
  without the old row id.
- Workflows: add a server → health → grant a key by server (the proxy's
  per-field merge simulated, devtools grant kept) → `mcp access` → delete
  names the stale grant; and incident triage: `mcp health` exit 2 →
  `mcp access --server <unhealthy>` names the affected keys.

## Guardrail coverage (`guardrails ...`, v0.11.0: `core/guardrails.py`)

Fixtures reproduce LiteLLM v1.106.3 (`tests/guardrail_fixtures.py`):

| Fixture | Source (litellm/ in v1.106.3) |
|---|---|
| `guardrail()` / `GUARDS` / `V2_LIST` | `GuardrailInfoResponse` / `ListGuardrailsResponse` (`types/guardrails.py`) as `list_guardrails_v2` and `get_guardrail_info` (`proxy/guardrails/guardrail_endpoints.py`) build them, masked by `get_masked_values` (`litellm_core_utils/litellm_logging.py`, reproduced by `proxy_mask`) |
| `V1_LIST` | `list_guardrails` → `_get_guardrails_list_response` (config.yaml only) |
| `created()` | `create_guardrail` → `GuardrailRegistry.add_guardrail_to_db` (`proxy/guardrails/guardrail_registry.py`): the request plus `guardrail_id`, unmasked |
| `deleted()` | `delete_guardrail_from_db`: `{"message": "Guardrail <id> deleted successfully"}` |
| `OVERVIEW` / `EMPTY_OVERVIEW` | `UsageOverviewResponse` / `_EMPTY_OVERVIEW` (`proxy/guardrails/usage_endpoints.py`, `guardrails_usage_overview`) |
| `DETAIL` | `UsageDetailResponse` (`guardrails_usage_detail`) |
| `LOGS` / `log_entry()` | `UsageLogsResponse` / `UsageLogEntry` (`guardrails_usage_logs`, `_usage_log_entry_from_row`) |
| `SUBMISSIONS` / `APPROVED` | `ListGuardrailSubmissionsResponse` (`list_guardrail_submissions`, `_row_to_submission_item`), `approve_guardrail_submission` |

- `guardrails list` is unchanged (config only); `--all` reads
  `/v2/guardrails/list` and tags rows `SRC=config|db`. No fixture secret (key,
  URL query) reaches any output, including the unmasked echo from `add`.
- `guardrails add`: dry-run prints the exact body and sends nothing. Refused
  before the proxy: a literal `--api-key`, a secret-named literal `--param`,
  bad modes.
- `guardrails update` PATCHes only the given fields (`default_on` inside
  `litellm_params`). A changed `mode`/integration is refused, as are config
  guardrails and "nothing to update".
- `guardrails delete`: dry-run, `--yes` gating, config refusal, keys whose
  metadata still lists the guardrail.
- `guardrails test`: passed / modified / blocked (exit 3, from a 4xx). Text can
  come from stdin. 404 and 401 are errors, not verdicts.
- `guardrails usage`: window params, exit 2 on `critical`, the empty overview,
  the per-guardrail detail by name, and the >366-day refusal before the proxy.
- `guardrails logs`: guardrail id, page size and action passed through. The
  `--limit` bound is checked, and snippets only appear with `--snippets`.
- `guardrails submissions/approve/reject`: redacted listing, approve gated by
  `--yes` and dry-runnable, reject ungated.
- Workflows: add (dry-run first) → test (blocked) → usage (critical) → turn
  `default_on` off → delete; and team review: submissions → approve → visible
  in `list --all`.
- MCP reviewer pin (`tests/test_mcp.py`): a team-less, org-less key whose
  owner is not an admin shows `(none)` / `reaches: []`. It still reaches
  `allow_all_keys` servers. Team, org and admin-owner keys show
  `(inherited: …)` with `inherits_from`. An unreadable user list assumes no
  admin. `users list` falls back to v1.106.3's `/user/list`.

## Results

`pytest tests --cov=cli_anything --cov-fail-under=70 -q` — 310 passed, 92% coverage (v0.11.0; `core/guardrails.py` 99%, `core/mcp.py` 97%). `ruff check cli_anything/` and `ruff format --check cli_anything/` (the CI scope) are clean, as are `tests/test_mcp.py`, `tests/mcp_fixtures.py`, `tests/test_guardrails.py` and `tests/guardrail_fixtures.py`; a repo-wide `ruff check .` still reports the 44 findings in `tests/test_core.py` / `tests/test_full_e2e.py` that predate this release, and `ruff format --check .` the same 5 files as main.

(v0.10.0: 262 passed, 90.3%. v0.9.0: 190 passed, 87.9% coverage.)
