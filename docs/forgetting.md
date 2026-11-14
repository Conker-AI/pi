# Forgetting a Pi conversation

Forgetting removes the content of one session, its fork descendants, and any
conversations that consumed it through selected project context from Pi's active
database. The preview lists the exact affected identifiers. Dependency expansion
is transitive, including descendants of those consumers. This scope includes
derived conversations because their summaries and replies can retain what
was said in the source. Pi does not yet track the provenance needed to safely
forget an individual message while keeping its derived replies.

## Owner operation

Forgetting always runs offline, with every Pi process stopped. There is no model
tool or turn-loop capability for it, and the runtime key cannot ask for it.

The owner can ask from the dashboard ("Delete forever"). That path uses the owner's
own credential (`X-Pi-Owner-Key`, distinct from the runtime key; the gateway asks
for the password first) and forgets nothing itself:

- `GET /forgetting/chats?session=ID` shows what deleting each chat reaches (counts
  of sessions, messages, turns and approval requests, never text) and the
  confirmation, plus the chats queued and the last results.
- `POST /forgetting/chats {"chats": [{"session", "confirmation"}]}` checks each
  confirmation against a fresh preview (409 `scope_changed` otherwise), writes the
  request beside the database (`pi.db.forget-request.json`: ids only), puts every
  session it reaches in the trash in the same step (so the chat leaves every list at
  once; restoring it answers 409 `deletion_pending` until the deletion has run), answers 202
  at once and stops Pi as soon as no turn, chat send or team run is in progress
  (new ones are refused for the moment until Pi is back). At the next start,
  before the Store opens, Pi runs the offline
  command below for each queued chat in a child process. A chat whose sessions
  changed since it was confirmed is refused, never widened. Results go to
  `pi.db.forget-result.json` (ids, states and receipt ids only).
- It needs a supervisor that starts Pi again when it exits (Compose's restart
  policy). Set `PI_SUPERVISED=true` there; without it the route answers 409
  `not_supervised` and the commands below are the only way.
- Cleanup that started and failed stays queued: the Store refuses to start until
  it is finished, the supervisor restarts Pi, and the next start finishes it.
- A run that fails before its cleanup starts (a timeout, a busy file) stays queued
  as `retrying`; Pi starts once more a minute later to try again. After three
  starts it is recorded as `refused` and the chat stays in the trash.

The confirmation below confirms scope; it is not a password.
An administrator capable of arbitrary Python or SQLite DDL already has database
authority; SQLite triggers are not a sandbox against such an administrator.

Use the updated Pi code. Stop **every** Pi process that uses this database,
including any older version, and stop external database writers. Older Pi
versions do not participate in the new runtime lease. Do not start an older
version against a database with forgotten sessions.

From the Pi repository, with its Python dependencies installed:

```bash
python -m pi.conversation.forgetting --db /absolute/path/pi.db preview --session ses_example
python -m pi.conversation.forgetting --db /absolute/path/pi.db forget --session ses_example --confirm CONFIRMATION_FROM_PREVIEW
```

For the standalone Pi Compose file, the equivalent commands use its data volume:

```bash
docker compose stop pi
docker compose run --rm --no-deps pi python -m pi.conversation.forgetting --db /data/pi.db preview --session ses_example
docker compose run --rm --no-deps pi python -m pi.conversation.forgetting --db /data/pi.db forget --session ses_example --confirm CONFIRMATION_FROM_PREVIEW
docker compose up -d pi
```

Build the updated image before these commands if it is not installed yet. The
command never contacts a model, ToolGate, or MemoryGate. It prints JSON: the
preview contains identifiers and a confirmation (and `approval_request_ids`, the
ToolGate requests the chat's parked turns wait on, and `tool_record_ids`, every approval request
and action its turns made, to forget in ToolGate); successful forgetting returns
a receipt. Review the session IDs before confirming. If messages, turns or fork
descendants were added after preview, obtain and review a new preview.

Only restart after a successful exit. If cleanup was interrupted, rerun the same
forget command. It finishes cleanup without changing the original receipt or
deletion time. If the original command was lost, `preview` on that same session
returns the confirmation again. Pi refuses startup while cleanup is unfinished.
An error never reports success.

## What is removed and retained

Removed: message payloads, affected session titles and summaries, turn approval
intent and arguments, routing explanations, and error/details text. Completed
actions are not undone. Turn outcome, `acted`, accounting, tool/approval IDs and
timestamps remain as execution facts. The session becomes `forgotten` and
cannot accept messages, fork, or resume any turn. Its parked turns disappear
from the resumable queues.
Its open loops (`open_loops`, [open loops](open-loops.md)) are deleted, the
promise words with them; while the chat waits in the Trash they are hidden.

Messages retain their original IDs, session IDs, sequence numbers, roles and
timestamps. Sequences are never renumbered and IDs cannot be replaced. Immutable
receipts record deletion ID, time, affected session IDs, message/turn counts
and a confirmation derived solely from identifiers. They contain neither the
removed text nor its hash. If a child was already forgotten, its original
receipt survives a later deletion of its parent.

Normal SQL UPDATE and DELETE of messages still fail. The offline command holds
an exclusive lifetime lease, removes the UPDATE trigger only inside the
redaction transaction, reinstates it before commit, and records the receipt
in that same transaction. Failure rolls all of that back. The DELETE trigger
stays active throughout. INSERT OR REPLACE cannot overwrite a message or receipt.

Pi holds a shared lease for each Store's full lifetime, including provider calls
between database connections. The lease lives in `pi.db.access.sqlite`, beside
the transcript, and relies on local filesystem SQLite locks. Do not use hard-link
aliases or independently mounted lock files for the same database. The lease
file contains no conversation content and can be recreated when all processes
are stopped. The durable unfinished-cleanup marker is in **pi.db**, so a database
backup retains it.

The command enables SQLite secure deletion, truncates the WAL, vacuums the live
database and checkpoints again before clearing that marker. It does not promise
forensic erasure from filesystem snapshots, storage-controller copies or swap.

## Evidence citations

Authenticated `GET /messages/{message_id}` returns the original live message, or
HTTP 200 with its stable envelope and an explicit tombstone:

```json
{
  "id": "msg_example",
  "session_id": "ses_example",
  "seq": 7,
  "role": "user",
  "created_at": 1789000000,
  "content": null,
  "content_status": "forgotten",
  "receipt_id": "del_example",
  "forgotten_at": 1789010000
}
```

Unknown IDs return 404. `GET /sessions/{id}` uses the same tombstones in its
message list. A citation holder should display **“Source forgotten by owner on
[date]; original content is unavailable.”** A tombstone cannot verify a claim,
and must not be presented as supporting evidence or invented replacement text.

This patch defines and serves that contract in Pi; it does not change MemoryGate
to fetch it, invalidate derived claims or delete its copies. Sparse citations,
including missing Russian evidence, do not alter the meaning of a tombstone.

## Hidden chats: one rule (2026-11-12)

A chat that is forgotten or in the Trash is hidden, and every read path asks one module for it
(`pi/conversation/visibility.py`): chat lists and search, the Inbox (tasks, handled actions,
memory items), artifacts copied from a chat, project sources, proposals, recall and memory
library views, Keep items, retries that would replay an inherited line, and calls whose source
chat is hidden. Still shown: the Trash view, the chat opened by its own link (its turns are not
refused), and approvals waiting for an answer. Restoring a chat from the Trash brings all of it
back; hiding deletes nothing. A new read path that can carry a chat's words filters with
`visibility.hidden_sql(column)` (for a column that may be empty:
`(column IS NULL OR NOT hidden_sql(column))`) and gets a test that trashes, checks, restores.

Follow-ups (2026-11-13):

- A chat made from a hidden chat is hidden with it: every fork, message branch, call, team role
  or agent call below it (`sessions.parent_id`, all the way down; they carry its title, summary
  or words), and the chat of an AI job Conker kept from it while the job is still Conker's. Once
  the owner adopts the job (`createdBy` owner) it is his: listed, running, its chat and notes
  shown. Only the chats he put in the Trash are in the Trash view.
- Agents, teams, AI jobs and team preparations Conker kept from a hidden chat leave every owner
  list (`/agents`, `/teams`, `/collaboration`, `/jobs`, `/definitions`, briefing items); each still
  answers by its own id.
- Recall is re-checked on use: a saved `conker.recall` result, a parked turn's saved recall and a
  retry (refused with `retry_source_unavailable` when its saved input recalled a now-hidden chat).
  A turn inside a trashed chat, opened by its link, keeps that chat's own memories; a forgotten
  chat is never exempt. One copy of the rule: `pi/conversation/hidden_records.py`.
- A proposal pass reads no chat hidden when it started (the tree included), so those chats hide
  none of its ideas. `visibility.hidden_since()` gives each hidden chat the earliest time it was
  hidden (a fork or a kept job's chat takes its source's time) in one query with no outer
  reference; `proposals.HIDDEN` builds it once per statement (`AS MATERIALIZED`) and compares it
  per message. Why: the earlier form named the pass's start time inside the recursive query, so
  SQLite rebuilt the hidden tree for every (proposal, message) pair; with 300 proposals and 100
  chats trashed before their passes, one `HIDDEN` query took 80 to 100 s, now about 0.3 s
  (`tests/test_visibility_since.py`). `visibility.hidden_by` stays as the one-column form.
  The window test then runs once per pass that holds proposals (not once per proposal) and reads
  the window's messages by time (`messages_by_time`, created in `proposals.SCHEMA`); a unary `+`
  on the hidden test stops SQLite from driving it from the hidden chats instead. Why: driven from
  the hidden chats it probed messages once per (proposal, hidden chat), and both only grow:
  forgotten chats stay in the hidden set for good (their words are gone, their ids are not) and
  proposals are never deleted. With 2000 forgotten chats of 4 each and 1000 proposals over a
  visible chat, one `HIDDEN` query took 3 to 4 s, now about 0.2 s. Driven by time it reads each
  pass's window once: about the messages since the last pass, the whole history only for the
  first pass (200,000 messages: 0.12 s).
- Activity reads "forgotten" only for a forgotten chat; a trashed chat's own runs read "available".
  A task or run opened by its own id shows its events, like a chat opened by its link.
- Restoring a chat whose parent (or any chat above it) is still in the Trash is refused and names
  that chat: restore it first, or both in one request. Otherwise the restored chat would stay hidden under it and look
  lost.
- An AI job Conker kept from a chat in the Trash does not run until the chat is restored or the
  owner adopts the job: its runs would write into a hidden chat he cannot see.
- `GET /turns/unreplied` leaves out hidden chats.
- Decision: chats whose model read a chat through a project source stay visible while that
  source is in the Trash. Forgetting takes them with it (`project_context_dependencies`, see the top
  of this page), but they are the owner's own other chats, not made from the source; hiding them
  would hide his work for a chat he only put in the Trash. The source stops feeding them at once
  (`project_sources.resolve` returns nothing for a hidden chat), and delete forever still forgets
  them at Pi's next start, as before.
- Decision: an agent-call chat ("Agent call: <name>") of an agent kept from a hidden chat stays
  under its visible caller, which recorded the call and owns that name.

## Limits that remain

This removes content from Pi's current store, not from everywhere it has been.
MemoryGate evidence, ToolGate records of other keys (gap 9 covers Pi's own), provider retention,
exports, browser caches and copies in unrelated sessions need their own deletion
paths. Pi refuses to resume the forgotten turn.

## What Pi does once it runs again (2026-10-29)

The command also queues, in the same transaction, what the chat left in other services; Pi does
it after it starts (`pi/conversation/forget_followups.py`, every 30 seconds,
`PI_FORGET_FOLLOWUP_SECONDS`). None of it can undo or block the forgetting:

- **Approval requests:** each request a parked turn of the chat waits on is withdrawn in ToolGate
  (`POST /v2/agent/requests/{id}/withdraw`, as the agent that asked), so its Inbox card, which
  quotes the action, goes. A request ToolGate no longer knows or the owner already answered (404,
  409) needs nothing.
- **Tool records** (2026-11-13, gap 9): every approval request and action the chat's turns made
  is sent to ToolGate's `POST /v2/agent/forget` (as the agent that made it, then as the key for
  what that agent does not know), which blanks their arguments and results and keeps the audit
  line (`pi/actions/tool_forgets.py`). It runs after the withdrawals: a request still waiting, or
  an action still running, is refused and tried again later. One ToolGate no longer knows is gone;
  Pi's key refused is `blocked` in Pi's health and still retried. Cron runs belong to their job,
  not a chat, and are not sent. A turn that parks again keeps only its latest request, so Pi
  records each one as it parks (`turn_approval_requests`); requests a turn parked on and replaced
  before this Pi version are not known and stay in ToolGate. A ToolGate without the route (before
  its release with it) is a retry, never forgotten.
- **Memories Conker kept on its own** from the chat (Keep results of `conker.remember`, recorded
  apart from the chat in `keep_results`) are forgotten in MemoryGate at their current revision,
  the same way as forgetting one memory by hand. A memory MemoryGate already held before the chat
  is left alone, and so is everything the owner approved himself.
- **Memory searches** Conker ran in the chat (`conker.recall`, saved in `recall_results`) are
  deleted; forgetting one memory clears every saved search result.
- **Agents and teams Conker made on its own** in the chat are archived (their stored
  configuration stays). They leave every owner list, and the model's `conker.run_agent` and
  `conker.create_team` treat them as unknown.
- **AI jobs Conker made on its own** in the chat are turned off and marked `turnedOff:
  "chat_forgotten"` at once (never deleted; waiting runs are withdrawn). They leave every owner
  list; a job the owner adopted before is his and is left alone.

A step that fails (service down, work running, another forget pending) is tried again with a
growing wait, at most hourly. Pi's health shows what is still owed as `forget_followups`
(counts only, `degraded` while something is blocked, such as Pi's key being refused, or waits on
a service that is not set up). The queues hold ids, never text.

Backups made before deletion still contain the content. A restore must replay
subsequent deletions before exposing a restored database. Cross-service deletion
propagation and receipt replay during restore remain integration work; do not
describe this command as system-wide forgetting. Fine-grained message deletion
and an authenticated browser owner interface are also deferred.
