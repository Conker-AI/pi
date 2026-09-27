# Templates and team preparation

These APIs persist configuration, immutable team revisions and preparations. Team
definition operations are available through the separate Pi owner-browser credential.
Templates, preparation creation, removal and execution remain recovery-admin operations.
Saving configuration does not dispatch turns, schedule work or grant permissions.

## Owner team contract

Exact routes under `/collaboration/teams` use `X-Pi-Owner-Key` (the gateway forwards
this server-side credential only after its ordinary owner authentication). Runtime
credentials cannot access them. All response models are strict, schema version `1`,
and use `authority: none`, `execution: configuration-only`, `contentIncluded`, and
`reference_validation: external-references-unverified`. Responses are not cached.

- `GET /collaboration/teams?limit=50&cursor={team_id}` returns compact metadata
  `{schemaVersion, results, nextCursor}`; the limit is 1-100. No definitions appear
  in collection summaries. Archived teams remain listed.
- `POST /collaboration/teams` accepts the existing strict `Team` definition below.
- `GET /collaboration/teams/{team_id}` returns the definition and live
  `agentReferenceState`: `available`, `unavailable`, or `selection-conflict`.
  `agentReferences` lists role IDs, canonical agent IDs and currently resolved
  revisions when validation succeeds. These are observations, not pinned execution
  snapshots; changing an agent does not rewrite the team or authorize a run.
- `POST /collaboration/teams/{team_id}/update` accepts `{expected_revision, definition}`.
- `POST /collaboration/teams/{team_id}/archive` accepts `{expected_revision, archived}`.
- `POST /collaboration/teams/{team_id}/restore` accepts `{expected_revision}` and
  unarchives the current definition. Both ways of restoring revalidate active agent
  references and narrowed selections; this is not rollback to an old revision.
- `GET /collaboration/teams/{team_id}/versions?limit=50&after=0` returns paginated
  revision summaries `{schemaVersion, results, nextRevision}`. Pass `nextRevision`
  as `after` to continue. `GET .../versions/{revision}` returns the exact historical
  definition with `historical: true`, without claiming live agent availability.

Team IDs are `team_` plus 32 lowercase hex digits. Roles reference `companion` or
`agent_` plus 32 lowercase hex digits. Missing agents, archived agents, widened
tool/memory selections and stale team revisions fail closed. Unknown request fields,
credentials, target argument objects and owner selectors are rejected. Malformed
stored definitions produce a static unavailable response without echoing their data.
Pi has one owner namespace per Store; this API adds no tenant selector or cross-store
lookup. Multi-owner isolation within one Store is not provided by the existing model.

All team writes and revision snapshots share one SQLite transaction. Triggers prevent
revision overwrite/delete/replacement and enforce advancing revisions on team changes.
Startup adds `team_definition_revisions` and backfills only each legacy team's actual
current revision. Earlier revisions cannot be reconstructed and return 404. Repeated
startup is idempotent; SQLite backups preserve the history and existing preparations.
No separate team model or execution snapshot is created by these owner routes.

## Recovery operations

The broader recovery routes are under `/collaboration` and require `X-Pi-Key`. GET the prefix to list
templates, teams and preparations. For either `/templates` or `/teams`, POST creates
a definition, GET `/{id}` reads it, POST `/{id}/update` accepts
`{expected_revision, definition}`, POST `/{id}/archive` accepts
`{expected_revision, archived}`, and POST `/{id}/remove` accepts
`{expected_revision}`. Updates use SQLite revision checks; stale writes return 409.
Names are unique per kind, including archived definitions. Removal hides a retained
tombstone and releases its name. Published templates and any prepared configuration
cannot be removed; archive them instead. Nothing deletes an underlying agent.

A template definition has `name`, `description`, and `agent` (the validated agent
configuration documented in [agents.md](agents.md)). POST
`/templates/{id}/publish` with `{expected_revision}` creates an immutable numbered
publication and increments the draft revision. Republishing an unchanged draft is
rejected. POST `/templates/{id}/instantiate` accepts
`{expected_revision, version, name, overrides?}` and saves a prepared configuration.
Overrides replace whole fields; tools and memory selections are never unioned with
defaults. Partial memory objects and arbitrary override fields are rejected. This
operation does not insert an agent or reserve its future name.

A team definition has `name`, `objective`, `roles`, `handoffs`, and `budget`, matching
the frontend collaboration contract. Each role names an existing active `agentId`,
has bounded instructions, tools, memory, context, and a budget. Role tool selections
must be subsets of the agent's selection. Memory may be narrowed to `none`, or stay
in the agent's scope with only a subset of selected records. Context is `task_only`
with no additional IDs or `selected` with explicit source IDs. Context IDs remain
unverified external references and confer no access.

Role IDs and case-insensitive names are unique. Handoffs connect distinct existing
roles, have unique IDs and directed pairs, declare `result_only` or
`result_and_citations`, and have positive `maxTransfers`. Cycles are allowed only
within these explicit limits. Total transfers cannot exceed team `maxHandoffs`.
Each role's `maxTurns`, `maxTokens`, and `maxCostCents` allocations contribute to a
sum that cannot exceed the corresponding team ceiling. Zero cost means no paid
usage. There are at most 12 roles, 30 handoffs and 100 references per selection;
turn/token/cost caps are 200, 1000000 and 1000000 cents, with at most 100 handoffs.

POST `/teams/{id}/prepare` with `{expected_revision}` revalidates current active
agents and saves the exact team definition and each agent's version/configuration
in one transaction. Later agent edits or archive operations cannot rewrite this
snapshot. Future preparations see those edits and can fail if a role's selections
are now too broad. Archived definitions cannot be edited, published or prepared.

All stored publications and preparations resist SQL update, delete and replacement.
Returned preparations declare `authority: none`, `execution: not-integrated`, and
`reference_validation: external-references-unverified`. External model/tool/memory
and context existence, privacy and live readiness still need authoritative checks.
Before future dispatch, an executor must revalidate current permissions, retain the
chosen snapshots, atomically reserve budgets and enforce handoff limits. These
configuration checks are not an execution ledger or permission grant.

Validation uses temporary SQLite databases without contacting services:

```sh
python -m pytest tests/test_collaboration.py tests/test_agents.py tests/test_projects.py -q
git diff --check
```
