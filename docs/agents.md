# Agent configuration lifecycle

Pi stores authored agent configuration independently of execution. The versioned
owner-control API accepts the distinct `X-Pi-Owner-Key`; the recovery `X-Pi-Key`
remains compatible and gateway runtime credentials are denied. The browser Gateway
exposes these exact routes under `/api/control/pi/` after owner login. Writes also
require the Gateway's operation-bound password verification.

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
1000 unique IDs. Memory scopes are `none`, `conversation`, `selected` and, for the
Companion only, `owner` (every chat's memory); only `selected` permits and requires
record IDs. The Companion starts on `owner`; the owner setting `memory.across_chats`
set to `false` narrows it to the current chat for every chat, and a team role may
read the current chat where its agent reads every chat. Extra fields and type coercion are
rejected with 422. Missing profiles/versions return 404; stale revisions, duplicate
names, Companion archive attempts and edits to archived profiles return 409.

Every successful edit/archive/restore appends a revision. A repeated archive state
with the current revision is a no-op. Stale revisions are rejected even for no-ops.
SQLite serializes writes, preserves history across restarts and forbids changes or
deletions to historical rows. Current profile identity is immutable. There is no
delete endpoint: archived profiles and their historical definitions remain readable
for eventual run snapshots and references. Renamed profiles retain earlier names in
their historical definitions; references must use stable IDs rather than names.

Initialization creates exactly one canonical `companion` identity, named Conker,
with a default configuration. The owner may edit that configuration through
`POST /agents/companion/update` with the current `expected_revision`; every edit
appends immutable history and is reflected in first-run setup evidence. Specialist
creation cannot create another Companion. The Companion cannot be archived or
deleted: its archive path is absent from the browser allowlist and storage rejects
the operation even through the recovery API. This stored default does not replace
the current runtime system prompt or owner character configuration.

Responses carry `schemaVersion: 1`. Agent IDs exposed through the browser are exactly
`companion` or server-generated `agent_` IDs containing 32 lowercase hexadecimal
characters. Profiles contain only bounded authored configuration, timestamps,
revision state, and explicit non-authority markers. They contain no provider
credentials, service URLs, approval grants, or execution tokens. Unknown request
fields and coercions are rejected rather than retained.

Responses explicitly state `authority: none`, `execution: not-integrated`, and
`reference_validation: not-performed`. External model/tool/memory IDs are requested
configuration, **not verified existence, readiness or permission**. Unlike the
frontend fixture, this isolated backend package has no authoritative joined catalogue
to validate selections against. Future dispatch must resolve references, intersect
current ToolGate/MemoryGate authority, snapshot the chosen revision and enforce
privacy before starting work. There are no session ownership changes, agent runs,
team preparation, task reassignment or scheduler effects here. Existing tasks still
identify the Companion; active-run/archive constraints belong with future binding.

The richer `/characters` package remains a separate recovery-admin surface in this
increment; owner-enabling agent configuration does not expose embedded portraits,
voice recordings, or imported character packages through the setup/dashboard
control plane.

## One agent runs another (`conker.run_agent`, 2026-11-12)

Conker's own tool `conker.run_agent` (`pi/turns/native_tools.py`, logic in
`pi/agents/agent_calls.py`) takes `agent` (an id or the exact name, any case) and `instructions`
(at most 8000 characters). Pi opens a new chat for that agent as a child of the caller's chat (so
forgetting the caller's chat forgets it too; its history starts empty), runs one turn there with
the instructions, and returns `{status: "done", callId, agentId, agentName, sessionId, outside,
message}`. The caller's chat stores only that pointer: each time its history is built, the model
gets the child's reply (cut to 8000 characters) read from the child chat, so forgetting or
trashing the child chat takes the reply away from the caller too ("gone"). The child chat's title
is "Agent call: <name>", never the instructions. A failed or parked child turn answers
`status: "failed"`, never an error. Every call is a row in `agent_calls` (caller chat and turn,
child chat, agent, depth, whether the caller had read outside content, state, the child's
submission and reply message).

- Default access is allow (the companion gets it; another agent when it is in its `toolIds`); if
  the owner's layers ask first, the call is refused and the model is told to use `conker.ask`.
- The child runs with the target agent's own toolkit and access, the caller chat's privacy, and
  memory always off: its one "user" message was written by a model. Of the caller chat's access
  switch only "ask" carries over; AI or Full was his loosening for that chat, never for another
  agent's toolkit, so the child starts at "default".
- The owner's Stop on the caller's turn stops every call it started (and theirs): Pi stops the
  running child turns, and each guard in a child turn checks the turns up its chain. A child that
  acted marks the caller's turn acted.
- A continuation of a child chat (an automatic or reviewed fork) is still that call's chat: depth,
  the chain, no owner request and outside carry over.
- No turn in a child chat has an owner request (`access.owner_text` is empty), so his words never
  approve anything there; if the caller had read outside content, every turn in the child counts
  as outside; the caller counts the reply as outside unless the result's `outside` is false.
- Refused: an unknown or archived agent; the caller itself or any agent up the chain; depth past 2
  (a depth-2 chat is not offered the tool); a fourth call in one turn; a 31st call in 24 hours;
  a team role's turn (its budget would not meter the child) and a run of an AI job.

Validation (temporary SQLite stores, no live services):

```sh
python -m pytest tests/test_agents.py tests/test_owner_preferences.py tests/test_store.py tests/test_tasks.py -q
git diff --check
```
