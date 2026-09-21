# Agent configuration lifecycle

Pi stores authored agent configuration independently of execution. This API requires
the existing `X-Pi-Key` administrator credential; gateway runtime credentials are
denied. No browser wiring is included.

| Method | Path | Request |
|---|---|---|
| GET | `/agents` | Lists current profiles, including archived profiles |
| POST | `/agents` | Agent configuration below |
| GET | `/agents/{id}` | Current profile |
| GET | `/agents/{id}/versions` | All immutable revisions in order |
| GET | `/agents/{id}/versions/{revision}` | One historical revision |
| POST | `/agents/{id}/update` | `{expected_revision, configuration}` |
| POST | `/agents/{id}/archive` | `{expected_revision, archived}` |

```json
{
  "name": "Evidence reader",
  "role": "Review evidence",
  "instructions": "Cite evidence and report uncertainty.",
  "modelId": null,
  "toolIds": [],
  "memory": {"scope": "conversation", "memoryIds": []}
}
```

Names, roles and instructions are required, trimmed text with limits of 80, 160
and 8000 characters. Names are case-insensitively unique across active and archived
profiles, including the Companion. Model IDs may be null. Reference IDs must be
nonempty, unpadded strings up to 200 characters; tool and memory lists allow up to
1000 unique IDs. Memory scopes are `none`, `conversation`, and `selected`; only
`selected` permits and requires record IDs. Extra fields and type coercion are
rejected with 422. Missing profiles/versions return 404; stale revisions, duplicate
names, protected Companion changes and edits to archived profiles return 409.

Every successful edit/archive/restore appends a revision. A repeated archive state
with the current revision is a no-op. Stale revisions are rejected even for no-ops.
SQLite serializes writes, preserves history across restarts and forbids changes or
deletions to historical rows. Current profile identity is immutable. There is no
delete endpoint: archived profiles and their historical definitions remain readable
for eventual run snapshots and references. Renamed profiles retain earlier names in
their historical definitions; references must use stable IDs rather than names.

Initialization creates exactly one protected `companion` identity, named Conker,
with a default configuration. Specialist creation cannot create another Companion,
and these specialist endpoints cannot edit/archive it. This stored default does not
replace the current runtime system prompt or owner character configuration.

Responses explicitly state `authority: none`, `execution: not-integrated`, and
`reference_validation: not-performed`. External model/tool/memory IDs are requested
configuration, **not verified existence, readiness or permission**. Unlike the
frontend fixture, this isolated backend package has no authoritative joined catalogue
to validate selections against. Future dispatch must resolve references, intersect
current ToolGate/MemoryGate authority, snapshot the chosen revision and enforce
privacy before starting work. There are no session ownership changes, agent runs,
team preparation, task reassignment or scheduler effects here. Existing tasks still
identify the Companion; active-run/archive constraints belong with future binding.

Validation (temporary SQLite stores, no live services):

```sh
python -m pytest tests/test_agents.py tests/test_owner_preferences.py tests/test_store.py tests/test_tasks.py -q
git diff --check
```
