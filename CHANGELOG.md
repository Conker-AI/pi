# Changelog

Versions are the module's own, not an API revision. A change to the shape of any
endpoint is a contract change and gets its own entry — replacing a module has to
be a decision with visible consequences.

## Unreleased

- **Which answers used a memory (R-U3, step 2).** Owner `GET /memory/{id}/uses` lists the turns that used one memory, newest first, with chat title, source and the memory's revision then; counts of uses and chats; `since` says when Pi began noting uses. Read only, ids and titles only (no memory words), so it works for a memory since corrected or forgotten. A forgotten or Trashed chat is left out and returns on restore. The browser contract allows the read.
- **Memory use notes (R-U3, step 1).** Pi keeps `memory_uses` (turn, chat, memory id, revision when known, source `context` or `recall`; ids only, no words) beside each saved recall package and `conker.recall` result. A correction or forget wipes the packages but not these rows, so "which chats used this memory" stays answerable. Forgetting a chat deletes its rows. Nothing reads them yet (route and notice follow); old turns have no rows.

- Cross-package imports: 11 of Pi's package-to-package import edges are gone (103 -> 92), by moves with
  the code unchanged (setup, models, memory, agents, calls, content and schedule no longer reach into each
  other's modules; `pi.turns.failures` keeps re-exporting what moved to `pi.core`). Behaviour, routes and the
  OpenAPI documents are unchanged (#253).

- **Docs: calls back into Pi (R-A14).** docs/boundaries.md names the two service cycles (Pi ->
  ToolGate -> Pi `/inference`, Pi -> MemoryGate -> Pi `/inference`), what stops them recursing and
  the known risks (waits that shrink inward, `/inference` running on after its caller left). No
  code change.

- **Open loops (R-M7).** A new table `open_loops` and three owner routes: `GET /open-loops`
  (`LoopPage`; browser read) lists promises across visible chats, soonest due first and undated
  last, with the owner's `today` and an `overdue` flag; `POST /open-loops` (`LoopAdd`) files one
  for a chat, optionally pointing at one of its messages; `POST /open-loops/{id}/state`
  (`LoopState`) marks it done, dropped or open again, also when its chat is forked or closed. A
  loop in a forgotten or trashed chat is neither listed nor changed, and forgetting the chat
  deletes it. The writes take the recovery key until the dashboard has a screen. No migration (a
  new table). docs/open-loops.md. (#251)

- **Memory upkeep candidates (read-only).** `GET /memory/upkeep` (owner session) passes on
  MemoryGate's upkeep list in camelCase as `UpkeepPage`. It holds exact and near duplicates, open
  contradictions, expired memories and ones not recalled for `unused_days` (1-3650, default 90),
  at most `limit` per kind (1-500, default 50). Each listed memory comes with its words and
  revision, so Fix and Forget can use the existing routes. A candidate that names a memory with a
  forget pending or done, or one made from a hidden chat, is left out whole, and that kind's
  `total` drops by those items. MemoryGate missing or silent is 503 `memory_unavailable`. A
  MemoryGate without the words (before memorygate#103) still answers, with no `memories`.

- **What Conker believes about you (read-only).** `GET /memory/beliefs` (owner session) lists one
  page of MemoryGate's memory-beliefs list (MemoryGate 0.9.0, memorygate#101) in camelCase as
  `BeliefPage`: origin (stated or concluded), confidence, evidence counts, lineage and open
  conflicts, with the filters and cursor passed on. A memory with a forget pending or done, or made
  from a hidden chat, is left out; MemoryGate missing or silent is 503 `memory_unavailable` (#248).
- Type check: the pyright baseline goes from 341 to 27 errors with type-only edits (annotations,
  casts, NoReturn on helpers that always raise, route path aliases at module scope). Behaviour and
  the OpenAPI documents are unchanged (#247).
- **History looks up named chats** (`pi/conversation/history.py`). `GET /sessions/history`
  takes an optional `ids=` (comma-separated, at most 50), so Home's "Jump back in" can ask for
  the chats it remembers instead of finding them only among the 50 newest. Hidden, trashed and
  forgotten chats never come back (archived ones only with `archived=include`; no `ids` with
  `trashed=only`); pages and cursors work as before. Without `ids` the reply is unchanged (#249).

## 0.25.0

- **A skipped update stays skipped when its release comes back** (`pi/owner/updates.py`).
  Withdrawing an offer newer than the latest release no longer deletes one the owner dismissed.
- **An update that rolled back is offered again** (`pi/owner/updates.py`). An offer marked
  installed by a check that ran mid-update reopens when a later report runs an older version and
  still names it as the latest, unless the owner dismissed it.
- **A memory search alone no longer marks a turn acted** (`pi/turns/recall.py`, `loop.py`), and
  `conker.run_agent` marks it acted only when the agent it ran acted. A failed answer after them
  is a failed turn, so the chat takes new messages.
- **A call stopped before its agent ran leaves no empty chat** (`pi/agents/agent_calls.py`). The
  empty "Agent call" chat and its call row are removed when the caller was stopped or the child
  turn never started.

- **A parked approval that expires ends** (`pi/turns/decision_sweep.py`). ToolGate keeps an
  approval it let expire as pending, so a parked chat turn or AI job waited for ever. Five minutes
  after `approval_expires_at`, and only when ToolGate still answers "pending", Pi ends the turn: the
  chat says the approval expired and nothing ran, the Inbox lists it under Handled, and an AI job's
  next run is no longer held back.
- **An update offer newer than the latest release is withdrawn** (`pi/owner/updates.py`), so a
  pulled release leaves no card that cannot be installed.
- **Your words are kept when they cannot be delivered** (`pi/turns/answers.py`). Words sent with
  a Change or decline, when the chat stays busy or the turn fails to start, become the chat's
  unsent message instead of a log line.
- **Cut and downgraded answers say so** (`pi/models`). An answer the provider cut at its length
  limit ends with a note; a turn that needed tools but fell back to a model without them says so;
  an over-long service answer is recorded as `too_long` with its usage (still a 503).
- **Provider failures keep their reason** (`pi/models/providers.py`, `direct.py`). An owner stop or
  a busy local model inside Ollama or a direct provider was recorded as "ProviderUnavailable".
  Anthropic direct now accepts the tool-format retry's late system note.
- **Interrupted team runs can be cancelled; role input is checked before the step is saved**
  (`pi/agents/team_execution.py`).
- **Tool requests keep path and pattern backslashes; a malformed ToolGate catalogue is "ToolGate
  unavailable"** (`pi/actions/tools.py`, `toolgate.py`).
- **The context-selection call's usage is kept when its answer is unusable**
  (`pi/context/retrieval.py`).
- **Gateway** (`gateway/`): /health asks the terminal sidecar each time and reuses its Pi and
  ToolGate checks for 10 s (it has no sign-in, so a flood makes no extra upstream calls); the
  ChatGPT runtime's "not confirmed" refusal reaches you as written; terminal work runs off the
  event loop; `conker apply` says an unknown outcome may already have been made (check before
  retrying); checking a session is a read (its touched time is written at most once a minute); a stalled or abandoned
  upload is `408 upload_incomplete`, not "Pi is unavailable".
- **A fork seeded by a summary keeps its parent's outside content** (`pi/turns/access.py`). An
  automatic fork's child starts from the model's summary of the parent, which carries what the
  parent read (web pages, tool results, attachments), yet it was judged on its own messages only.
  Now a chat that starts from a summary counts as having read outside content whenever its parent
  (and so on up) did, so his words no longer approve an action and remembering asks him.
- **Team runs work on every provider, not only OpenRouter** (`pi/agents/team_execution.py`). Only
  OpenRouter reports a price, so a team call on a local model, a ChatGPT or Claude plan or a direct
  key was stored as unknown usage, its answer was thrown away and the run blocked. A call that
  reports its tokens but no price now counts tokens only (`maxTokens` is its stop; money counts only
  reported prices, as for the daily cap). A missing price from OpenRouter is still unknown usage.
- **The first-run rehearsal stays done after you use Conker** (`pi/setup/rehearsal.py`). Its
  receipt named the newest conversation, so the next chat turned the rehearsal back to "ready"
  and the setup step to degraded. A receipt now holds while its own conversation (the newest one
  that had ended when it was recorded), memory review and approval still stand; forgetting that
  conversation or changing the memory choice still undoes it.

- **A decision Pi was not told about still continues the work** (docs/inbox-answers.md). A
  decision made with ToolGate's admin key, or while Pi was down, used to leave the parked turn or
  cron job run waiting for ever. Pi's decision sweep (`pi/turns/decision_sweep.py`, every
  `PI_DECISION_SWEEP_SECONDS`, default 60) reads each waiting request's status with its own key and
  takes the owner route's step; it never reads the note, so a "no" just closes.
- **A cron job waiting on the owner** (docs/jobs.md). Cancelling a run that waits on its ToolGate
  approval now queues that request for withdrawal in the same transaction, so its card leaves the
  Inbox. An AI job whose turn parks on an approval holds its next run back until he answers,
  instead of dispatching into the busy chat and ending `outcome_unknown` (which blocked the job
  until reconciled); reconcile keeps `waitingForApproval`, and the Inbox no longer shows or counts
  such a run as handled while it waits.
- **Change requests and answers fail closed** (docs/inbox-kinds.md, merge 3 is not doing). A
  change request from a forgotten or trashed chat is not listed, not counted against the overall
  limit, and answers 404 on every owner route until the chat is restored (one with no chat is the
  owner's own and always shows). An unknown kind is refused (`unknown_kind`, 409) instead of being
  made as a memory. An answer that is exactly one of the offered options counts as chosen even if
  the client did not say so, and an answer turn whose question link is gone counts none of its
  text as the owner's request.
- **The Inbox leaves out what a hidden chat made.** `GET /inbox` no longer lists suggestions from
  a forgotten or trashed chat, nor names an AI job Conker kept from one in Upcoming or Handled
  (an adopted job is the owner's and still shows). A turned-off job is no longer Upcoming.
- **One notices table for plan alerts and AI job notes** (docs/inbox-kinds.md, merge 1).
  Migration 0006 moves `plan_alerts` and `agent_job_notes` into `notices`, keeping every id, and
  drops the two tables. Routes, response bodies, Inbox items and auth are unchanged. A database
  this migration has run on needs the pre-update snapshot to go back to an older Pi.
- **Memory proposals are retired** (docs/inbox-kinds.md, merge 2). `GET /inbox` no longer lists
  kind `memory`, and the admin-key routes `/memory-proposals` (list, create, get, `/decision`,
  `/reconcile`) are gone from the API and docs/pi-openapi.json. No Pi code, dashboard or CLI used
  them; the owner corrects memory through `/memory/corrections`, and Conker's "remember" is a
  change request. The `memory_proposals` table stays so forgetting a chat or a memory still
  scrubs old rows (`pi/memory/proposals.py`). Contract change: the OpenAPI component names
  `pi__content__artifact_models__Create` and `pi__schedule__proposals__Decision` become
  `Create` and `Decision` now that nothing else shares those names.
- **Migration ledger** (docs/migrations.md). Schema upgrades are named steps recorded in a new
  `schema_migrations` table, each run once in one transaction with its record; the five guarded
  start-up upgrades moved there unchanged. A database a newer Pi upgraded is refused (Pi does not
  start, and the forget queue and the host forgetting command write nothing), so a rollback
  across a migration needs the pre-update snapshot. `GET /health` gains `schema_migration` (the
  last recorded id); additive field.
- **Each tag gets a GitHub Release page and an SBOM.** The Release page's notes are the
  version's CHANGELOG section. Every image carries an SPDX SBOM (BuildKit attestation) inside
  the image index, and the signed build provenance covers that index digest. Check one with
  `gh attestation verify oci://IMAGE@DIGEST --owner Conker-AI`.

- **One daily cap over every paid model call** (docs/spending-cap.md). OpenRouter's paid models
  and the direct-key providers pass a cap in owner preferences (`spendingCap`: money from the
  reported cost and tokens, default $5 and 2M tokens a day); once reached, paid calls are refused
  as a model refusal (roles fall back) until the owner's next local day. `GET /inference/spending`
  is the meter. New preference field with a default; preferences saved earlier read it.
- **Conker searches memory itself.** `conker.recall` (Look, allowed by default) searches memory
  under the running agent's own credential and scope, as turn recall does; never in a private chat
  or a team role. Results live in `recall_results` (cleared when any memory is forgotten, deleted
  with the chat), not in the chat's messages, and count as outside content unless every item is
  the owner's own words.
- **Pi tells the owner when it is broken** (docs/self-check.md). A background self-check reads
  the `/health` checks every 5 minutes; one that stays unavailable or degraded for 15 minutes
  files an Inbox item (kind `health`), and its recovery shows in Handled (`health-recovered`).
  New `GET /self-check` and `POST /self-check/alerts/{id}/dismiss`; no change to existing routes.
- **Conker update offers and the gateway's update action.** `/updates` keeps what the host found
  when it checked for a newer Conker release: one offer per version (dismissing one is remembered;
  running that version or a newer one closes it) and one result per update receipt (applied,
  rolled back, held or failed). Only the recovery credential reports (`POST /updates/report`, the
  host's daily check); the owner-control credential reads and dismisses. The Inbox lists the newest
  open offer and every update that did not end applied; an applied one is handled. The gateway's
  host action `update.run {version}` asks for the password again (428), then hands the update to
  the host helper.

- **Conker can ask for an agent or a team.** `conker.create_agent` and `conker.create_team` file
  change requests of the new kinds `agent` and `team` (the table is rebuilt once on start to allow
  them; rows kept); approving makes the agent (default model, no per-tool access, chat memory
  only, only tools the caller could use, never create or delegation tools) or a linear team of
  existing agents with a small budget. His typed words never approve one; his chat or agent
  loosening can, as Keep: listed in Handled, archived when the chat is forgotten.
- **A Morning check-in is on by default.** When Pi starts with its scheduler it makes, once per
  install (new or upgraded), the AI job "Morning check-in": daily 08:00 in the owner's time zone,
  the companion says what needs him today or replies NOTHING. `createdBy: "conker"` (proactive
  budget, quiet hours and the proactivity switch apply; never his words), fixed id
  `job_beca06ba345a8b3170a4d8e2473fe00f`, recorded in the owner setting `rhythm.seeded`; turning it
  off never brings it back. It does not count against Conker's own jobs a day.
- **An agent can run another agent.** Conker's own tool `conker.run_agent` runs the named agent on
  the model's instructions in a chat of its own (a child of the caller's chat, memory off, no owner
  request, only "ask" of the caller's access switch, outside if the caller read outside content);
  the caller's model reads its reply from that chat, so forgetting or trashing it removes the reply
  too. The owner's Stop reaches the child. At most depth 2, no loops, 3 per turn and 30 a day, not
  from team roles or AI jobs (`agent_calls`, forgotten with either chat). docs/agents.md.
- **Deleting an imported chat erases its words in MemoryGate too.** The deletion records the
  chat's MemoryGate evidence ids (`import_evidence_erasures`, ids only), and the listener sender
  erases them before it offers anything new, so the words, their analyses and the memories made
  from them go with the chat. (#190)
- **Delete forever holds.** A queued deletion that fails for a passing reason stays queued and Pi
  tries again a minute later (refused after 3 starts); a queued chat is hidden at once, and
  restoring it answers 409 `deletion_pending`. (#193)
- **One rule hides forgotten and trashed chats** (`pi/conversation/visibility.py`). Lists, search,
  recall, the memory explorer, proposals, Inbox tasks, handled actions, artifacts and other chats'
  model input all leave them out; the Trash view and the chat's own link still show them. (#202)
  It now also covers every chat below a hidden one, the chat of a kept AI job until it is adopted,
  kept agents, teams and jobs, recall results and retries, and a turn in a trashed chat keeps its
  own memories. (#210)
- **Owner routes describe their replies in OpenAPI** (D-01), so generated clients can type them;
  what Pi sends is unchanged (`responses=described(...)` documents a reply, it never re-validates
  or filters it): owner settings, preferences and model configuration (#229), search (#233),
  Inbox questions, change requests and handled actions (#235), chat name and output (#236),
  characters (#237), plan usage and setup (#238), skills and tool drafts (#239), chat imports
  (#240), the memory library, corrections, forget, remember and delete forever (#242), the chat
  itself: session, settings, context, summary and the new-chat draft (#244). Contract
  change: `GET /system/files/roots` declares the two shapes it sends, chosen by `mode`
  (`RootCatalogue` becomes a union; the bytes sent are the same) (#241); some existing model
  classes got clearer component names (`ChangeRequestView`, `Character*`), the old names stay as
  aliases.
- **Forgetting a chat forgets its ToolGate tool records** (gap 9). The chat's approval request and
  action ids are queued with the forgetting (`toolgate_forgets`, ids only); once Pi runs again it
  asks ToolGate to forget them (`POST /v2/agent/forget`, toolgate#105) and retries anything still
  pending, running or not confirmed. Every request a turn parked on is recorded, not only the
  last. (#231)
- **docs/boundaries.md**: turn states, the three credentials, and what Pi and MemoryGate each
  hold. (#230)
- **Repository tooling, no behaviour change.** The shared .editorconfig, pre-commit hooks,
  CODEOWNERS and Renovate config; a pyright type check whose per-file baseline may only shrink; CI
  fails below 91% coverage; the mutation drills run beside the tests (CI 13.5 -> 7 min); a table of
  every Inbox kind (docs/inbox-kinds.md); the turn mixins declare the attributes they use (types
  only). (#195, #201, #204, #207, #232)

## 0.24.0

- **Chats go to a Trash bin.** `POST /sessions/trash` moves chats to Trash or restores them with
  the session alone (no password); trashed chats leave history (`trashed=only` lists them), search,
  universal search and recall. Delete forever still asks for the password but answers 202 at once:
  the request is queued and Pi restarts to forget offline only when no turn, submission or team run
  holds the database.
- **Output settings gain a language level.** A chat's output settings carry `level`
  (`auto`/`strict`/`plain`/`normal`/`deep`) for how short and simple answers are; auto follows the
  owner's `chats.language_level` (default normal) and normal adds no instruction. Old saved rows
  load with `level: auto`.

## 0.23.0

- **Every model input has hidden code points removed.** `pi/core/untrusted_text.py` strips
  format and bidi controls, Unicode tags, variation selectors, Hangul fillers, private-use and
  unassigned code points in `Message.__post_init__`, so memory recall, chat imports, files,
  webhooks, project context, tool results and team handoffs are all cleaned; transcripts keep what
  arrived. ToolGate's copy pins the same fingerprint of the removed set. Ollama and the Claude plan
  adapter now label tool results as outside content like the other providers.
- **After outside content, remembering always asks the owner.** In a turn that read outside
  content, `conker.remember` files an Inbox request at every access level and the AI reviewer is
  never asked. The owner's own typed "remember" still saves at once with Undo.
- **A team handoff keeps the outside mark.** A role's `owner_text` is only the run's input message;
  `outside_content` follows the step's inputs (the previous role's turns, recursively, and each
  selected message's turn), so content one role read cannot pass as the owner's request to the next.

## 0.22.0

- **Chats can be deleted forever.** `GET/POST /forgetting/chats` (owner control plane) previews
  and queues chats for permanent forgetting. Each confirmation is checked against a fresh
  preview; a chat whose sessions changed is refused, never widened. Pi then restarts and the
  offline forgetting command erases the queued chats before the Store opens; unfinished cleanup
  stays queued, so the Store keeps refusing to start. Needs a supervisor that restarts Pi
  (`PI_SUPERVISED=true`); without one the route answers 409. Asks for the password in the browser.
- **Several memories can be forgotten at once.** `POST /memory/forget-many` forgets up to 50
  memories one at a time, each its own idempotent forget, and stops at the first refusal.
- **An imported chat can be deleted for good.** `POST /imports/chats/{id}/delete` removes the
  chat, its messages and its waiting offers with secure delete, keeps ids and counts only, and a
  later import skips it. The memory sender re-checks each offer under a lock the deletion also
  takes. Asks for the password in the browser.

## 0.21.0

- **Chats can be archived and restored in bulk.** `POST /sessions/archive {sessions, archived}`
  (1-100 chat ids) archives or restores them in one call, all or nothing: an unknown id refuses the
  whole request with 404. It is an ordinary, undoable chat write, so the signed-in session is enough.
  Archiving hides a chat from the list; it forgets nothing. The gateway relays it at
  `/api/control/pi/sessions/archive`.
- **Chat history filters and sorts.** History takes `archived=exclude|only|include` (default
  `exclude`, so archived chats leave the list) and `sort=newest|oldest|name`, and returns
  `archived_at`.

## 0.20.0

- **The password is asked only where it protects something.** A signed-in session is enough for
  ordinary writes that can be undone (chat features, memory edits, imports, tool runs, drafts,
  settings, signing in or out of a plan, server actions, revoking sessions). The password proof is
  still required for forgetting, the owner's preferences (sign-out time, quiet hours, budgets),
  backups' destination, job webhooks, adopting a job, scheduled budgets, entering a provider key,
  the paid-model policy, Claude API billing, sending a vault secret, the terminal, and anything that widens what
  Conker may do (granting a tool or workflow to chats, publishing to run without asking). Level-3
  approvals keep asking (0.19.0). The server owns the list (`PASSWORD_WRITES`); a write without a
  needed proof answers 428 `verification_required`.
- **A sign-in stays unlocked while the owner uses the page.** `POST /auth/activity` (empty body),
  which the dashboard sends on the owner's own clicks and keys, restarts the idle window; polling
  and an untouched tab never do (L-03 stays closed). After the window (`idleTimeoutMinutes` in the
  owner's preferences, default 15) with no password and no activity, the session ends. Never past
  the absolute lifetime from sign-in.

## 0.19.0

- **The gateway asks for the password only on serious approvals.** It reads each request's
  `approval_level` from ToolGate (0.13.0) before deciding. A yes needs the password proof only at
  level 3 (delete, pay, host controls); levels 1 and 2 need the signed-in session, and the
  dashboard adds a red confirm for level 2. A no or a dismissal never needs it. Fails closed: if
  ToolGate is unreachable, an older ToolGate has no level, the answer is odd or no owner key is
  set, the level counts as serious and a yes without proof answers 428 `verification_required`.
- **Strangers guessing the password can no longer lock the owner out.** A signed-in session
  confirming an action and a browser that signed in before (`__Secure-conker-device`, path
  `/auth`, kept only hashed, replaced at each sign-in, at most 20) each have their own budget of 5
  per 5 minutes; unknown browsers share the old one. Every wrong password is recorded for 30 days
  (at most 500); `GET /auth/alerts` returns the unseen count and the newest 50 as
  `{at, origin, seen}`, never the password tried or the address, and `POST /auth/alerts/seen`
  marks them seen.
- **The images take Debian's security fixes newer than their base** (apt, CVE-2026-109518).

## 0.18.1

- **An approved AI job is saved in one go.** Approving an `ai_job` request writes the job, its
  chat, the chat link, the decision and the Keep link in one transaction. A failure at any step
  leaves the request open and nothing made (before, a job could exist with no chat, or with no
  Keep link, so forgetting its chat did not turn it off), and the job limit is re-checked under
  the write lock. A kept memory's Keep link is saved with its decision the same way.
- **A webhook's payload is saved with its run.** The job worker could start the turn before the
  payload was written; it is now written in the run's own transaction and kept until the run
  ends, so a repeated dispatch replays the same turn instead of answering `request_conflict`.
- **Replaying a call turn that is still running answers 409 `request_in_progress`** instead of
  its half-finished state.
- **Streams end honestly.** A streamed OpenAI-style answer cut off before `[DONE]` or a
  `finish_reason` is refused instead of accepted as complete; a slow-drip ChatGPT stream has a
  total 70 s deadline; a browser upload through the gateway times out after 120 s without data.
- **Replies that are not JSON objects are refused as unavailable** (Claude plan, decisions,
  ChatGPT control), and `NaN` or `Infinity` in a relayed service answer is refused. After a
  failed POST to the ChatGPT or host provider control, the answer says the operation may have
  taken effect; only a failed GET says nothing was retried.
- **Terminals.** Closed terminals are evicted, so the 1000-terminal cap cannot fill up; the sweep
  closes outside the lock and carries on past a failed close; resize takes positive integers
  only; SIGTERM closes the sidecar's open connection, so shutdown does not hang.
- **Smaller fixes.** Setup status reports a step degraded when the setup store cannot be read
  instead of failing; an open plan alert gets its reset time, so it closes at the reset;
  compaction coverage is cumulative; an artifact whose source message holds malformed JSON reads
  as `source-changed`; research model calls still `pending` an hour after a crash are marked
  failed at start; linking a made item to its chat is retried and otherwise answers
  `keep_failed`; `gateway health` uses `--port`.
- **Tests.** The audit drill's baseline runs every file a case selects (29/29 mutants caught);
  new tests cover every project-context refusal, the attachment schema triggers, the retired
  workflow tables under forgetting, the search excerpt cutter and the direct-provider docs.

## 0.18.0

- **The owner's browser reaches the v1 chat features.** Output settings, chat title and auto name,
  retry, fact check, branching from a message and branch history, response families (select,
  judge, vote and their reads), and the chat's context (policy, transfer to a new chat, summary
  edit, restore and draft, compaction, decisions and threads) now take the owner-control key as
  well as the recovery key, through `/api/control/pi/...`, for a well-formed chat, message, item
  or request id only (`browser_contract.CHAT_READS` / `CHAT_WRITES`). Each write needs the signed-in
  session, CSRF, no query string, a body Pi's own strict model accepts (checked at the gateway
  before the password proof) and a proof bound to that exact operation. Turn snapshots and context
  selections stay recovery-only. Contract change: these operations list `X-Pi-Owner-Key` instead
  of `X-Pi-Gateway-Key` among their headers (the runtime key never reached them).
- **Opening a chat reads memory delivery once, not once per turn.** `GET /sessions/{id}` scanned
  the memory outbox and validated every receipt again for each turn; it now does that once and
  reuses it for the session and every turn. The response is unchanged.
- **Service calls cut off by a restart are marked failed.** A service model call that was running
  when Pi stopped stayed `pending` until the 30-day cleanup, so usage showed calls that were not
  running. Startup now marks each one `failed` (`failure_kind` `interrupted`, with an end time); it
  is never retried.
- **Search ranking asks about search results.** The `search-ranking` role now tells the decision model
  to rank search results and labels each choice "Result …"; it used memory wording before. Memory
  ranking is unchanged.

## 0.17.0

- **Direct Gemini, xAI, Mistral, DeepSeek and Groq.** Five more direct text providers, each over its
  vendor's OpenAI-compatible Chat Completions endpoint with a Bearer key: provider IDs `gemini`,
  `xai`, `mistral`, `deepseek`, `groq`, keys `PI_GEMINI_KEY`, `PI_XAI_KEY`, `PI_MISTRAL_KEY`,
  `PI_DEEPSEEK_KEY`, `PI_GROQ_KEY` (or their `_FILE` forms). Same spending opt-in, redaction and
  readiness rows as OpenAI. They count as hosted for interactive answers on Auto. Contract change:
  the gateway's host provider operations accept the five IDs, and a host status may list them
  beside the three it must list (openrouter, openai, anthropic).
- **Tool texts the model reads call the owner they/their.**

## 0.16.0

- **Helper roles can follow the answer model ("Auto").** A role assignment with `followAnswer: true`
  is saved with the answer role's model and backups, filled in again on every save, so changing the
  main model moves every Auto role with it; the role keeps its own time limit. Off is still
  `enabled: false`, roles added by an upgrade still arrive Off, and the answer role cannot follow
  itself. Contract change: `followAnswer` (default `false`) on every role assignment.

## 0.15.1

- **One sent offer no longer hides that another one failed in the same pass.** `Offers.drain_once`
  sent offers one at a time and cleared the status problem after every success, so a later offer
  that was acknowledged erased the "MemoryGate did not answer" note an earlier offer in the same
  pass had set, and the owner's status showed no problem while an offer still waited to be retried.
  The problem is now cleared only by a pass in which nothing failed for a passing reason.
- **Queue submits and receipt reads decode the settings snapshot once.** `_files` parsed the same
  snapshot JSON four times per submit, edit and validation, and `_view` decoded the submission
  settings a second time for the research mode after already decoding them; both now parse the
  text once and reuse the result.
- **A quiet-hours pause keeps the last look's failure.** The paused look in `agent_triggers.check`
  saved the watch row without `failure`, so a tool that didn't answer in the evening read as
  having no problem after the first overnight pause, although nothing had looked since. A pause
  now writes back the failure the last real look recorded.

- **A declined turn that already acted no longer blocks its queue for good.** A queued message that
  ran a tool and then parked on an approval the owner declined stayed `claimed`, so it could be
  neither removed nor resumed and every later queued message waited behind it; `reconcile` now marks
  it `failed` like any other stopped turn, so it can be removed and the queue continues.
- **A repeated setup memory review answers instead of failing with a 500.** Retrying the review
  with a new `requestId` at the same choice revision computed the same evidence digest, passed the
  `request_id` replay check, and hit the table's unique digest constraint, so a review that was
  already recorded raised `IntegrityError`. The same evidence is now treated as a replay and the
  status is returned without inserting.
- **A malformed settings document never fails an Ollama answer.** Reading the local model settings
  caught only a broken database, so a missing `owner_settings` row, `vals` that was not JSON, or
  JSON that was not an object (an array, say) raised and failed the whole chat answer over a
  preference. Each now reads as an unset document and sends nothing, so the answer comes with
  Ollama's own settings.
- **Model profiles stay fast as judgements pile up.** Every dashboard load re-read the whole
  `response_judgements` and `response_votes` tables once per configured model and ran one point
  query per score and per vote, so the work grew with models x judgements x scores. Both tables
  are now read once per load, each judgement's JSON is parsed once, and all message ids are
  mapped to their models in chunks of at most 500; the computed profiles are unchanged.
- **Two webhook calls at once can no longer both start a job.** The 30-second gap was checked
  before the run started and written after, so two calls arriving together both passed it, and if
  the first run ended quickly the second started too. The gap is now claimed in one statement
  before the run starts; a call that finds the job busy gives it back, as before.
- **An export that is too long says so.** The size check in `office_exports.render` now matches the
  limit the export worker actually reads, so a payload between the two limits is refused up front
  with "too long" instead of starting a worker that answers `invalid`.
- **A deep-research turn with no recorded question now ends cleanly when approved.** Resuming
  it after approval read the turn's input message without checking it exists and crashed with a
  `TypeError`, leaving the turn claimed. The approved search still runs and is recorded; the turn
  then ends as acted without a reply (or failed, if nothing ran), so the reply can be asked for.
- **A provider key file swapped after its check is no longer read.** `PI_*_KEY_FILE` was checked
  to be a regular file and then opened by name, so a symlink or named pipe put there in between was
  followed or waited on. The open now refuses a final symlink where the OS can (`O_NOFOLLOW`), never
  waits on a pipe, and checks again that what it opened is a regular file.

- **The definitions list no longer reads every team preparation.** Building the team and template
  rows of `/definitions` parsed every preparation snapshot and every published version ever stored,
  so the route got slower as those append-only tables grew; it now reads each record's own columns
  in one query. The rows it returns are unchanged.

- **A model that starts charging falls back instead of failing.** Refusing a paid OpenRouter
  model (with paid models off) raised a plain error, so a role's fallback models and a turn's
  next free model were never tried and the call failed. The refusal now counts as this one model
  being unusable, like a gated one: the next candidate, which passes the same check, answers,
  and the skipped reason is recorded. A setup test of such a model reports
  `provider_unavailable`. One malformed entry in OpenRouter's model list is left out (so calling
  it is refused) instead of failing every call, and a list that is not a list is reported as the
  catalogue being unavailable.
- **Forgetting a memory scrubs the proposals about it.** `POST /memory/forget` removed the
  memory in MemoryGate but left its old text, the proposed text and the reason in every memory
  proposal for it. Those proposals are now scrubbed and hidden in the same transaction, as
  forgetting their conversation already did.
- **Webhook text cannot pose as Pi's own words.** An AI job started by its webhook got the sent
  JSON inside a `<webhook>` label, and a value containing `</webhook>` closed it early. `<` in
  the sent JSON now reaches the model as `\u003c` (the same JSON value). Inbox notes from AI jobs
  drop terminal and other control codes, and a model probe whose reported model name holds them
  fails as `invalid_response`.
- **Saving a large character no longer stalls other requests.** Saving, importing or restoring a
  character package (up to 66 MiB) parsed, validated and wrote it on the event loop, so event
  streams and call audio waited behind it; that work now runs in a worker thread. Responses and
  limits are unchanged.
- **Building a turn's history looks up attachments once, not once per message.** Every selected
  history message opened its own SQLite connection to `message_attachments`, almost always to
  learn it had none, so a 1,000-message chat opened 1,000 connections before the model was
  called, on every turn and every retry. The message ids that have attachments are now read once
  in batches and only those messages are walked.
- **A retried fact check or judge pays for one model call.** Two requests with the same
  `request_id` that arrived together both called the model and kept one answer. The later one
  now waits for the first and replays its result.
- **The Inbox reads its approvals once.** Building the Inbox scanned the approval queue twice
  and opened a new database connection for every turn in it; it now reads the queue once on one
  connection and shows the same items.
- **The artifacts list no longer reads every version of every artifact.** Building the library
  listing decoded the body of every stored version (up to 100) only to drop them all; it now reads
  each artifact's first and last version, and the listing shows the same summaries as before.
- **A turn names at most five attachments.** `POST /sessions/{id}/turns` refused nothing above
  five while the queue and the dashboard already stopped there; it now answers 422 at
  six, so a direct caller cannot reserve a long list of uploads in one turn.
- **Attachments keep the order they were sent in.** `bind()` read reserved attachments back
  `ORDER BY attachment_id`, so a message's attachments appeared in the model input and the
  transcript in random order instead of the order the owner sent them; it now reads them back
  in insertion order.
- **The memory outbox no longer scans every past message each second.** The once-a-second poll for
  due memory work now reads pending rows from a partial index instead of the whole table, whose
  receipt rows grow with the owner's history. What is sent, and when, is unchanged.
- **Health says when the voice is down.** A speech capability that is set up but whose last
  call failed was reported as `not_configured`, which counts as healthy, so `/health` said `ok`
  while calls had no voice. It is now `degraded` and named in `degraded`.
- **Pausing a call keeps the model chosen for it.** A call update re-read its own body in a way
  that marked every field as sent, so pausing, resuming, switching mode or a channel cleared the
  call's model choice. Only a field the request names changes now; `modelId: null` still clears it.
- **A call turn paused after its reply is recorded complete.** When the owner paused or ended a
  call just after the reply was saved, the request was marked `failed` beside a delivered
  message. It is now `complete`, its voice is skipped and marked `failed-or-interrupted`.
- **A cited answer re-checks only the files it cites.** When a final answer cites an attachment
  passage, Pi re-reads and re-extracts each cited file inside the write that saves the answer, to
  prove the passage still matches; it used to do that for every file of the turn (a PDF each time),
  holding the write lock while it ran. Citations that are kept are the same.

- **Agent identities come back after a ToolGate restart.** A 404 or 405 from ToolGate's identities
  route turned per-agent scoping off until Pi restarted; Pi now asks again after five minutes.
- **A garbled ToolGate status is reported, not raised.** `action_boundary` in Pi's status is
  `unavailable` (`unreadable status`) when ToolGate answers 200 with a body that is not a JSON
  object; the status endpoint used to answer 500.
- **An AI job request is approved once.** Approving takes the request before the job is made, so a
  second approval or a decline arriving at the same time (a double-click, two tabs) answers 409
  `decided` instead of making a second cron job or leaving one behind a declined request. If the job
  cannot be made, the request stays open.
- **A long reply no longer breaks the call screen.** The browser call view caps each event's text
  at 16,000 characters, but replies have no limit, so one long answer made every
  `/calls/browser` route fail. The view now clips that text with an ellipsis; the event's source
  still names the full message.
- **Pi refuses a gateway credential equal to its admin key.** `PI_GATEWAY_KEY_SHA256` set to the
  admin key's digest gave the browser path recovery authority (the admin key is checked first).
  Startup now refuses it, as it already did for the owner and service keys.
- **Inbox cards carry no terminal control characters.** Change-request titles and summaries, and the
  names, labels and description of a drafted tool, drop control characters (except tab and newline in
  summaries), so a model reply or a chat message cannot retitle or clear the owner's terminal when
  `conker` prints the Inbox. Titles stay on one line. A memory request's text stays exactly as asked.
- **pypdf 6.21.4.** Fixes seven published denial-of-service advisories in 6.21.2 (crafted PDFs
  that loop, exhaust memory or forge page counts). Pi reads attached PDFs with it.
- **Naming says so when the routing role is off.** `POST /sessions/{id}/title/auto` answers 503
  `not_configured` without calling a model when the routing role is switched off, as it does when
  nothing is saved (it used to try, fail and answer `unavailable`).
- **Ordered backup models for each role.** A role's `fallbackModelId` becomes
  `fallbackModelIds`, up to 8 backups tried in the owner's order after the role's model fails
  (contract change: reads return the list). Saved settings and older clients that send one
  `fallbackModelId` still work: it replaces the list. Waits count every model a role may try.
- **Next-message suggestions.** `POST /sessions/{id}/suggestions` asks Jev (the routing helper)
  for 1 to 3 messages the owner might send next, from the chat's last exchange; the count
  defaults to the owner's Suggestions shown setting. Nothing is saved. The owner key may call it.
- **Download parameters are in the gateway's OpenAPI.** The artifact and chat-answer download
  routes now list their `format` query parameter (and the artifact's optional `version`, 1 to
  100) in `docs/gateway-openapi.json`, so generated clients see them. Behaviour is unchanged.

## 0.15.0

- **Artifact downloads take a version.** `GET /api/control/pi/artifacts/{id}/download` accepts an
  optional `version` (1 to 100), so an earlier version can be exported to Word, PDF, PowerPoint or
  Excel; the chat answer download still refuses it.
- **The CLI runs any tool by hand.** `conker apply tools` takes `{"operation": "run_tool", "id",
  "action_id": "owner_…", "args", "approval_request_id"?}` and sends it to ToolGate's
  `/v2/owner/tools/{id}/runs` with the chat's key, as the browser's Run does; a tool that asks
  still makes an approval request, resumed with the same action id.

## 0.14.0

- **An AI job's opening message stays out of memory.** Each run's first message (the preamble,
  the run's date and time, why it started, any webhook payload and the job's instructions,
  which for a job Conker made the model wrote) is no longer queued for MemoryGate as if the owner
  had typed it. The job's chat still recalls memory, and what the owner types there is his own as
  anywhere. The message view shows it as `excluded` with no reason code. An opening message an
  older Pi had queued and not yet sent is withdrawn on upgrade.
- **His own memories no longer silence his words.** Recalled memories make a turn count as outside
  content only when at least one is not the owner's own: MemoryGate's `source_type` `user` (his
  statement) or `owner_statement` (made from his chat messages, each cited message checked
  against Pi's own records) are his; anything else, or no readable source, counts as outside, as
  do entities, episodes and a briefing (`pi.memory.store.others_wrote`).
- **The owner can take a job Conker made as his own.** `POST /jobs/{id}/adopt`
  `{"expected_revision": n}` (owner credential) sets `createdBy` to `owner` and records
  `adoptedAt` in the job (shown in the job view, kept by edits). Its later runs count as his
  (his words, no proactive budget), and forgetting the chat it came from no longer turns it off.
  A job that is already his is refused (409 `already_owner`).
- **The drafter asks in the chat.** When the drafter needs a detail for a tool request made in a
  chat, the card no longer fails: it becomes `payload.status: "needs_detail"` with the drafter's
  `question`, and Conker posts the question in that chat (a stored message; `askedIn` names the
  chat once it is posted). The owner's next message there is the answer: Pi takes it before any
  model call, drafting goes on with the questions and answers so far (`payload.conversation`, at
  most 16 entries, at most 4 questions) and Conker's stored reply says so. From the Inbox,
  `POST /change-requests/{id}/retry` `{"answer": "..."}` does the same (409 `not_asking` when no
  question waits). An empty retry now keeps the answers; a new description starts over. Requests
  without a chat (`/tool-drafts`) still fail with the question.
- **Old workflow paths retired.** `POST /workflow-drafts/draft`, the `/workflow-proposals`
  routes (offers included) and their Inbox items (`kind: "workflow"` and `"workflow-offer"`) are
  gone; the command line drafts through `POST /tool-drafts`. The `workflow_proposals` and
  `workflow_offers` tables stay (dropping them needs a migration): nothing writes or lists them,
  and forgetting a chat still clears its rows.
- **The browser gateway reaches owner files, imports, server actions, tool settings and
  lockdown** (`gateway/relay.py`, `file_routes.py`, `host_ops.py`; contract in
  `docs/browser-auth.md`). One streaming file relay serves downloads
  (`GET /api/control/pi/artifacts/{id}/download?format=docx|pdf|pptx|xlsx|csv|json` and the chat
  answer download; session; only the asked type, a checked attachment name, at most 16 MB,
  no-store, nosniff; Pi's refusal JSON passes through) and imports
  (`POST /api/control/pi/imports/{chatgpt|claude}/preview` with the session,
  `.../{digest}` with the password; raw `conversations.json` up to 128 MB, a body with another
  digest refused before Pi has all of it). `GET /api/host/ops`, `GET /api/host/ops/{id}`,
  `POST /api/host/read` (session; `services.status`, `backup.list`) and `POST /api/host/ops`
  (password bound to the body; `backup.verify`, `services.restart`, `backup.run`) go to the host
  helper over the provider-control socket, its receipts and refusals unchanged.
  `GET`/`PUT /api/owner/tool-settings` (PUT with the password) and `GET`/`POST
  /api/owner/lockdown` (on with the session, off with the password) go to ToolGate's owner
  routes. `GET /api/owner/tools/{id}/runs` (session) and `POST /api/owner/tools/{id}/runs`
  (password bound to the body; the chat's execution key) run any tool by hand through ToolGate's
  owner runs. `POST /api/control/pi/jobs/{id}/adopt` (adopting a job Conker made) passes the
  control relay with the password bound to its body. Browser contract: an import preview is a
  session-only write.
- **`conker apply requests` can answer the drafter.** A `retry` takes an optional `answer`
  (1..4000 characters), sent as `POST /change-requests/{id}/retry` `{"answer"}`; with a
  `description` as well it is refused before Pi is called (`gateway/mutation_models.py`).

## 0.13.0

- **A cron job's page can show what it does and who does it.** The owner's job view
  (`GET /jobs`, `GET /jobs/{id}` and every route that answers with a job) adds `instructions`,
  the job's own instructions in full, and `agentName`, its agent's current profile name (null
  when Pi has no such agent). `instructionsConfigured` stays. Owner routes only: the gateway's
  runtime key still cannot read `/jobs`.
- **AI job runs know the date.** Each run's opening message says when it started in the job's
  time zone, for example "This run started on Monday, 26 October 2026 (2026-10-26) at 12:00,
  Asia/Jerusalem time (the job's time zone).", so "today", "this week" or "only in the first week
  of the month" work. It is the time Pi took the run up, so a repeated dispatch still replays.
  In a run of the owner's own job only the saved instructions count as the owner's words
  (`pi.turns.access.owner_text`); the preamble, the date and why it started are Pi's.
- **Real deliverables: artifacts and chat answers export to DOCX, PDF, PPTX and XLSX**
  (`pi/content/markdown_blocks.py`, `export_documents.py`, `export_pdf.py`, `export_slides.py`,
  `_export_worker.py`). Markdown keeps its structure: headings, numbered, bulleted, task and
  nested lists, quotes, code blocks, rules, tables with their alignment, bold, italic,
  strikethrough, inline code and links (only http, https and mailto stay clickable; a
  javascript: or other link stays its plain label). Images are never fetched. XLSX puts each
  table on its own sheet as text. `GET /artifacts/{id}/download` gains `format=pdf|pptx` and
  DOCX/PDF/PPTX for table and code artifacts; the new owner-only
  `GET /sessions/{sid}/messages/{mid}/download?format=docx|pdf|pptx|xlsx` exports one completed
  answer under the same source and privacy checks as saving it as an artifact. The owner-control
  key may now fetch both downloads directly (`OWNER_DOWNLOADS`, for the host CLI); they stay out
  of the gateway's JSON relay. Files are made in an isolated worker with time, CPU, memory, size,
  page (500), slide (300), nesting (12) and table limits, each refused with a fixed `export_*`
  code. New pinned libraries: python-pptx 1.0.2 (MIT), markdown-it-py 4.2.0 (MIT), ReportLab
  5.1.0 (BSD); the image gains `fonts-dejavu-core` for Cyrillic and Greek in PDFs. A Markdown
  artifact's DOCX now reads single line breaks as spaces (as the chat shows them), and its
  sources are a short list instead of a JSON block.
- **Import from ChatGPT and Claude** (docs/imports.md). Owner routes `POST /imports/{chatgpt|claude}/preview`
  (saves nothing) and `POST /imports/{chatgpt|claude}/{digest}` (exactly the previewed file) read
  an export's `conversations.json` strictly, with size and count limits, and keep each chat as an
  archived, read-only chat in its own tables (`GET /imports/chats`, labelled "Imported from
  ChatGPT/Claude", the export's ids and times kept), idempotent by the export's chat and message
  ids. The owner's own messages are offered to MemoryGate through an import listener
  (`PI_MEMORYGATE_IMPORT_SOURCE`, `PI_MEMORYGATE_IMPORT_KEY`) as outside content, never as owner
  statements; nothing is offered when memory was skipped at setup or new chats start private.
  Nothing runs during an import, and imported text never reaches a turn except as recalled
  memory, which counts as outside content.
- **A failed answer save no longer leaves the chat running until a restart**
  (`pi/turns/restart.py`). A database error at the final commit is tried again three times
  (0.25, 1, 3 s). If it still fails, the request says the answer is kept and a small settle,
  running only while an answer waits, tries again every 15 s and saves it once the database works,
  with no restart and no new model call. A stop or steer sent meanwhile ends the turn as the
  startup settle would; a save the database refuses, or one still failing after 40 tries while
  other writes work, ends the turn `failed` (or `acted_no_reply`) in plain words. A try that
  committed but lost its reply is recognised, so nothing is saved twice.
- **Local model settings are read** (`pi/models/local_settings.py`), on every Ollama request:
  chats, helper roles and `/inference` alike; hosted providers are unchanged. Keep loaded
  (`models.local.keep_loaded`, minutes kept to 5..1440, or `"until_restart"`) is sent as
  `keep_alive`; Local context size (`models.local.context`, kept to 2048..32768 and never above
  `PI_OLLAMA_CONTEXT_LENGTH` or `OLLAMA_CONTEXT_LENGTH` when Pi's environment has one) as
  `options.num_ctx`; CPU threads (`models.local.threads`, 1 to the host's threads) as
  `options.num_thread`. Unset, malformed or 0 threads sends nothing, so Ollama's own settings
  (`OLLAMA_KEEP_ALIVE` 24h, `OLLAMA_CONTEXT_LENGTH`) still apply. A context too large for the
  machine makes local answers fail as an unavailable model, never Pi (docs/routing.md).
- **Sign out when idle is enforced** (`session.idle_timeout_minutes`). The gateway learns the
  owner's `idleTimeoutMinutes` from every preferences document it relays (a read, or a
  password-verified save) and keeps it in its own database, so a restart or Pi being down never
  loosens it. A browser stays unlocked that long after the password was last entered; every
  signed-in browser follows at once. `GATEWAY_IDLE_TIMEOUT_SECONDS` (default 30 minutes) is the
  host's ceiling: a longer choice, and 0, mean the host's limit, which also applies until the
  gateway has seen a document. A save always wins; an older read may only shorten the window.
- **The access a new chat starts with** has a password-checked home: `newChatAccess`
  (`default`, `ask`, `ai` or `full`, default `default`) in the owner preferences, which the
  gateway saves only with a fresh password proof. `pi.owner.preferences.new_chat_access(db)`
  reads it inside the transaction that creates a chat the owner starts; chats that exist, AI
  job chats and forks keep their own switch. Creating chats does not call it yet.
- The dashboard may be installed as an app: its pages allow the web app manifest
  (`manifest-src 'self'`). Built files under `assets/` whose names carry a content hash are
  cached for a year (`immutable`); everything else stays `no-store`.
- **Conker's own actions need the owner (security review 2026-10-29, F-20 to F-27, F-29).**
  `conker.schedule` asks by default (his settings for an agent or a chat can still loosen it;
  his own words no longer make a job), jobs record `createdBy`, and a job Conker made on its own
  reserves from the daily proactive budget before each run and never counts as his words; no
  AI-job run may schedule or draft a tool; Conker's requests make no job while 20 AI jobs are on
  and at most 3 a day on its own. A private chat is never offered `conker.remember` or
  `conker.schedule`, and a job approved from one gets a private chat. Outside content now also
  covers recalled memories, attachments, project sources and answers to Conker's questions, and
  a webhook payload marks its whole chat (new table `outside_sessions`; none of that chat's
  messages goes to memory). In AI decides the hard stop is the owner's Inbox card template
  (send, publish, ship, delete, pay; read from ToolGate's tool list when it lists `card`), not the
  reviewer's own answers, and an MCP server's descriptions never reach the reviewer. Keep
  memories go to MemoryGate with `origin: "conker"`; Keep results are linked to their chat
  (`keep_results`), and forgetting a chat turns off its Keep jobs, forgets its Keep memories and
  withdraws its parked turns' ToolGate approval requests once Pi runs again
  (`approval_withdrawals`, health check `forget_followups`). At most 3 of Conker's requests wait
  per chat and 20 in all. `memory.across_chats` is on only when unset or exactly true.
- **Chat settings are read** (`pi/conversation/chat_preferences.py`). Private by default
  (`chats.default_incognito`) turns memory off in new chats, which also keeps them out of search;
  Helper models in new chats (`privacy.helpers`) off turns helpers off in new chats. Both only
  add privacy, are saved with the chat when it starts (existing chats and forks keep theirs), and
  a choice sent for that chat still wins. Name chats automatically (`chats.names.auto`) off stops
  the name after the first answer (asking for one still works); Name style (`chats.names.style`:
  short, descriptive, emoji) sets the naming rule. Queue messages (`chats.queue`) off refuses new
  queued messages with `queue_off`; Queued messages (`chats.queue_max`, 1 to 5, default 5) sets
  how many wait. Messages already queued stay and run. Largest upload (`files.upload_limit`, 1 to
  500 MB) refuses a bigger file with 413 `upload_limit`, before streaming when its length is
  declared; with no value set there is still no limit (the 2026-10-18 decision). A value of the
  wrong type or out of range counts as the default.
- **Interactive answers.** With the owner setting `chats.interactive_answers` (Settings, Chats,
  Interactive answers: Auto, On, Off; Auto by default) a turn is taught the catalog of parts
  Conker's dashboard draws from an ```` ```openui ```` block: tables, charts, choices to tap, short
  forms, steps, progress and callouts. Auto teaches it only when every model that may answer is
  hosted (Anthropic, OpenAI, OpenRouter, ChatGPT, Claude Code), so a local model keeps plain
  Markdown; never in a call or a JSON-only chat. The catalog prompt
  (`pi/conversation/interactive_answers.txt`) is generated by Conker's dashboard and pinned here.
  The choice is read once per turn into the output snapshot (`output.interactive`, only when On
  or Off, so earlier snapshots still match); answers are stored as written.
- **Small models' tool requests get through more often.** One narrow repair, approved by the
  owner: a whole reply that fails to decode only because of backslashes that start no JSON escape
  (`\$` in `echo exit=\$?`) is decoded again without just those backslashes, and the result goes
  through every normal check (offered tool, exact `{"tool", "args"}` shape, the owner's access
  layers, ToolGate). Nothing else is ever repaired. The tool list shows each tool's inputs as a
  JSON template (`args: {"description": "<text>"}`) instead of `args: description: string`,
  which small models copied as a `"description: "` key.
- **The retry note names the mistake**: an invalid escape or the decoder's own words for broken
  JSON, text before or after the request, an input name with something added (named from the
  tool list, e.g. `"description"`), or a request narrated and quoted in a code block. The last
  is new: prose that says the model is about to act ("I'll use...", "Let me run...") around
  exactly one quoted request for an offered tool is retried once like a request wrapped in prose
  and never run; an explanation that only shows the format is still an answer. A second invalid
  reply fails the turn, as before.
- **Memories per answer** (`memory.per_answer`, 1 to 50, default 8) now sets how many memories an
  answer asks MemoryGate for; MemoryGate takes at most 30 per request, so a larger value asks for
  30. Memories whose text repeats an earlier one (ignoring case and spacing) are dropped before
  the model reads them, the first kept, and counted in the package's
  `retrieval.pi_duplicate_memories` receipt. Search still shows every record.
- **Research, plan alert and speaking speed settings are read.** Each only lowers a fixed limit
  and changes nothing while unset: `research.deep_searches` (a deep research's actions, at most
  4, saved with its plan), `tools.web.results` (results per search, at most 8),
  `research.recent` (a search's age: a year or a month), `context.web_page_limit` (Pi's cap on a
  page read, at most 12000 characters) in research turns; `money.plan_windows` (the 5-hour and
  weekly windows, matched by the runtimes' labels; other windows always count) for new plan
  alerts; `voice.speed` (50 to 200 percent, sent as the speech server's `speed`) for spoken
  answers in calls.
- **An answer survives a restart.** Pi holds each answer (new table `held_answers`, forgotten with
  its chat) before the final commit, so an answer produced just before Pi stopped is saved at the
  next start instead of lost; a stop or steer the owner sent still refuses it, as it would live.
  One of Conker's own tool calls (`conker.*`) cut off mid-way is settled from Pi's records instead
  of leaving the chat waiting forever on ToolGate, which never saw it: filed means the chat waits
  only for its reply, not filed means nothing was done and the result says so. Nothing runs twice.
  A turn cut off by a restart now says in plain words what happened and what to do next.
- **MemoryGate and ToolGate use the owner's model roles.** `POST /inference` (service key in
  `X-Pi-Service-Key`, hashes in `PI_SERVICE_KEYS_SHA256`) answers one prompt for the calling
  service's own role, `memory-sorting` or `tool-text`, through Pi's normal role call. Until the owner
  picks a model for one, it uses the local model: nothing leaves the server on upgrade. Each call's
  usage is recorded without its text; `GET /inference/usage` lists it. See docs/inference.md.
- A model role may wait up to 10 minutes (was 2). Saved settings keep their wait; when nothing was
  saved, a role whose model is local waits up to 5 minutes, and background thinking honors its
  role's wait instead of stopping at 2 minutes.
- **Conker drafts tools in the background.** `conker.draft_tool` files the tool request at once
  (payload status `drafting`) and the chat answers right away; no model call or reviewer waits in
  the turn. A drafting worker drafts one request at a time with the `drafting` role (the answer
  role until one is picked), each call within its role's time limit and cancelled when Pi stops,
  from the at most 8 capabilities that match the description (published workflows included). It
  checks the draft with ToolGate's `POST /v2/editor/check` and repairs it up to twice; the ready
  payload adds `steps` in plain words. A draft running when Pi stopped is failed on start.
- New owner routes: `POST /tool-drafts` (idempotent on `request_id`), `POST
  /change-requests/{id}/retry` and `POST /change-requests/{id}/published`, which stores Conker's
  "“<name>” is live. Want me to run it now?" in the chat that asked. The gateway proxies all three
  for the signed-in owner without a password, and passes `conkerMayUse` on the ToolGate publish
  route (password verified, with Conker's key when true).
- Pi no longer makes workflow offers in chat or tells the companion it cannot draft; offers already
  made stay until accepted or dismissed.
- **The owner's settings reach every chat.** Instructions for every chat
  (`context.instructions`) and Simpler wording (`accessibility.simple_words`) become one system
  message right after the agent's own instructions; the profile (`you.call_me`, `you.pronouns`,
  `you.about`, `you.people`, `you.goals`, `you.units`, `you.wake`, `you.sleep`, `you.week`,
  `you.focus`, with the quiet-hours time zone) becomes one short message after it. Project
  sources "None" (`context.project_sources`) keeps a chat's selected project sources from the
  model. Each value is checked again (type, choice) and cut at a hard cap (instructions 4000
  characters, the profile 8000); a value not set adds nothing. A turn reads them once, when it
  starts, and keeps them in its execution snapshot (key `owner`, present only when something is
  set), so a replay or retry sees the same words and a change reaches the next turn only. The
  owner's text is framed as data: it grants no permission, and approvals still judge only the
  owner's message in the chat.
- **Tests keep their files in pytest's `tmp_path` only.** `tests/conftest.py` stops and fails any
  test that writes into the repository (a file opened for writing, a new folder, a rename, a
  delete, a SQLite database that is not read-only), naming the path, in every pytest-xdist worker;
  `__pycache__` and `.pytest_cache` are left alone. A run whose `--basetemp` or temp directory is
  inside the checkout is refused before any test starts. No test wrote into the repository; the
  writes came from runs given `--basetemp=.test-tmp/...` and from the three mutation drills, which
  now keep their copies in the OS temp directory (the audit drill also puts `--basetemp` beside
  its copy, since the copy brings the guard along).

## 0.12.0

- **Conker remembers across chats by default.** The Companion's memory scope starts on `owner`
  (every chat's memory). An install whose Companion was never edited moves to it once, as a recorded
  `updated` revision; a profile the owner saved keeps his choice. The owner setting
  `memory.across_chats` (Settings, Memory, Remember across chats) set to `false` keeps every chat to
  itself. Saving a chat's settings no longer changes what it recalls: before, a chat whose settings
  were never saved read every chat and one whose settings were saved read only itself.
- A team role may read the current chat where its agent reads every chat (narrower is allowed).

## 0.11.1

- **Small models get a second try at a tool request.** Besides a request wrapped in prose, two more
  mistakes small local models make are asked for again once, with the exact format spelled out:
  request JSON that does not parse (often unescaped quotes), and a tool's arguments sent without
  `{"tool": ..., "args": ...}` around them, which until now reached the owner as raw JSON. Only
  an exact fit counts as bare arguments: every key is an input of one offered tool and its
  required inputs are there. Nothing from a rejected reply runs or is kept, the retry never sees
  it, and a second invalid reply fails the turn as before.

## 0.11.0

- **Owner settings document:** `GET`/`POST /owner/settings` keeps every registry setting that has
  no other home, by its id, revision-checked and shape-checked (docs/owner-settings.md). Saving
  needs only the signed-in session: it holds preferences, never authority.
- **Contract change:** `/owner/preferences` now takes the owner credential as well as the
  recovery key, so the Settings screen can show quiet hours, proactivity, budgets, plan alerts and
  the idle timeout. Saving through the gateway asks for the password.

## 0.10.0

- **A separate, stronger reviewer.** The check before an action runs without the owner has its own
  model role, `reviewer` (older settings gain it switched off; until he picks a model, the answer
  model reviews). In AI decides it never lets an action that reaches a real person or cannot be
  undone through without him: the reviewer answers both questions and Pi asks him on anything but
  a clear no, whatever the verdict. His own words and Full access are unchanged. The reviewer is
  told when the run read outside content. See docs/access.md.

## 0.9.0

- **His Inbox answer continues the work.** After ToolGate records his decision, the gateway calls
  `POST /approvals/{id}/decided` (from the Inbox and `conker apply approvals`). Do it resumes the
  parked turn at once. Don't ends it `cancelled` and the chat takes messages again. A note with a
  no (Change) becomes his next message in that chat. Before, the action waited until he opened
  the chat, and a no left the chat stuck. See docs/inbox-answers.md.
- **`conker.ask`:** Conker asks him a question with 2-4 options it writes. The question waits in
  the Inbox (`GET /asks`, `needsYou` kind `ask`); the chat is never held. His answer arrives as
  his message in that chat (`POST /asks/{id}/answer`, `/dismiss`; CLI `apply asks`). A tapped
  option never counts as his own words for the access layers. At most 3 open per chat, 20 in all.
- `POST /change-requests/{id}/decline` takes an optional `reply`, sent to that chat as his next
  message.
- Gateway: `POST /api/owner/tools/{id}/card` picks a tool's Inbox buttons (ToolGate's fixed
  templates); needs the password proof.

## 0.8.0

- Gateway: `GET`/`POST /api/owner/tools/{id}/access` shows and switches whether Conker may use a
  tool at all (ToolGate's owner tool access, on the chat's own key). Writes need the password
  proof, like the other owner access switches.

## 0.7.0

- **Agent identities:** Pi calls ToolGate as the running agent (`X-ToolGate-Agent`) after defining
  that agent's toolkit there, so ToolGate enforces each agent's tools, not only Pi. The companion
  keeps the key's full view. Approval resumes and action checks use the same agent, since ToolGate
  binds approvals to it. An older ToolGate without identities keeps the key, and Pi's own filter.

- **Access layers:** each action resolves from the owner's own settings, the more specific
  winning: this chat's switch (`settings.access`: default, ask, ai, full), the agent's
  `toolAccess`, the tool's own default. Guards: the agent's toolkit, ToolGate's floor (sensitive
  tools always ask) and outside content (one step stricter; his words stop counting; Conker's own
  Keep actions go to the reviewer). AI decides is Pi's reviewer (unsure means ask); his own typed
  request counts as approval when it asks for exactly this action. Pi sends ToolGate the outcome,
  honoured only for a policy-delegate key and never on the floor. Conker's remembering and
  scheduling now happen at once. Actions that ran without asking are listed at `/access/handled`.
  See docs/access.md.
- `GET /tools` lists each tool's default access (`authorization`) and whether it is on the owner's
  floor (`sensitive`). The gateway forwards ToolGate's owner access routes: `GET /api/owner/access`,
  `POST /api/owner/access/delegate` (the chat's own key) and `POST /api/owner/tools/{id}/sensitive`,
  both writes password-verified.

- **Conker prepares, the owner verifies:** three tools of Conker's own, `conker.schedule`,
  `conker.draft_tool` and `conker.remember`, let a chat (or an AI job) ask for a cron job, a new
  tool or a memory. Each only files a change request; nothing exists until the owner approves it
  (`/change-requests`, approval password-verified). Tool drafts reach ToolGate only through the
  owner's browser, whose verified save in Tools is then recorded (`.../saved`). Memories are
  stored word for word through MemoryGate's correction capability (needs MemoryGate with
  `/runtime/corrections/remember`). `GET /tools` lists Conker's own tools. See
  docs/change-requests.md.

- **AI jobs:** a cron job can have its agent carry out the owner's instructions with tools
  (`target.kind: "agent"`), on any cron timing, in its own chat. Tools behave exactly as in chat:
  get tools run on their own, set tools wait for the owner's approval in the Inbox. Repeating jobs
  skip quiet hours. A reply other than `NOTHING` adds an Inbox note linking to the chat. New owner
  routes `POST /jobs/agent`, `GET /jobs/agent/notes` and `POST /jobs/agent/notes/{id}/dismiss`;
  the browser can create AI jobs only, never raw tool jobs. See docs/jobs.md.
- **AI jobs that start from a tool:** with `watch`, an AI job starts when a get tool (any MCP or
  other tool that needs no confirmation) has something new, checked every 5-1440 minutes. Pi
  keeps only a fingerprint of the answer. A tool that needs approval each time stops the watch
  with a plain reason. See docs/jobs.md § Starting from a tool.
- **AI jobs that start from a webhook:** `POST /hooks/{job_id}` with the job's secret in the
  `X-Conker-Hook-Token` header starts it once (one call every 30 seconds, never while running).
  The secret is shown once and kept as a fingerprint; what the caller sent reaches the agent as
  information, never instructions, and is deleted once read. Owner routes
  `POST /jobs/{id}/hook` and `/hook/revoke`. See docs/jobs.md § Starting from a webhook.

## 0.6.1

- Add the optional `drafting` model role. The workflow builder uses it when the owner
  enabled it with a model, and the answer role otherwise, so chat can stay on a small local
  model while drafts use a stronger one. Older saved settings gain it disabled.
- The workflow builder waits up to 10 minutes for the model, past the answer role's
  120-second limit: on a CPU-only local model a whole draft takes minutes. Chat answers keep
  their own limit.
- The owner-control credential may read `GET /plans/usage` (browser path
  `/api/control/pi/plans/usage`), so the dashboard's Home can show plan allowance. Dismissing
  a plan alert stays with the recovery credential.

## 0.6.0

- **Conversations.** Thinking is explicit and cancellable and continues reliably across
  providers, with concurrent thinking roles; a durable Stop interrupts the foreground
  inference it owns. Native Claude Code and Codex subscription transports run on the host,
  with owner-bound Claude session recovery and Codex saved-session discovery. The composer
  keeps private draft intent and promotes it atomically into a new chat. Chats get names
  (owner rename and an automatic name), searchable history pages that apply the shared
  privacy policy before paging, per-chat output settings, a readable context policy and
  summary, reviewed compaction that folds messages into the summary, and decisions and open
  threads. Messages name the turn that consumed or produced them.
- **Answer quality.** A blind judge scores answer versions and the owner can vote on them;
  fact checks run against an answer's own sources; read-only model profiles come from
  recorded outcomes; Auto settings proposals come with a complexity hint.
- **Memory.** Complete memory evidence stays within the context byte budget and empty
  metadata stays out of prompts; source-bound delivery is acknowledged; the owner can
  correct a memory's text through a reviewed, durably recoverable flow. Forgetting a chat
  now also removes its review data, summary coverage and workflow drafts.
- **Models and setup.** Exact local model tags are verified, and setup activates a model
  only after exact readiness. Hosted catalogue health is reported apart from inference
  readiness, and selection evidence apart from escalation and fallback. Ollama's cached
  prompt usage is kept. Identical Laya decisions are reused within one turn attempt, and a
  stopped turn cancels its exact decision request.
- **Skills, tools and MCP.** Explicitly selected skills are frozen into each turn without
  granting tools, and a skill replacement is bound to the reviewed registry identity. MCP
  setup and access stay behind exact owner verification, and complete MCP argument schemas
  reach model context. Malformed tool proposals are refused instead of recorded as answers.
- **Inbox and workflows.** An Inbox shows what happened and what needs the owner, including
  plan usage alerts. The talking model drafts editor workflows from the owner's words or
  from one of their chat messages; drafts wait in the Inbox and only the owner publishes.
  One summary list covers everything the owner organizes.
- **Files.** Files and pictures shared in a chat are kept: uploads stream to disk inside
  Pi's data volume (so volume backups include them) and the database keeps metadata, size
  and SHA-256. The per-file, per-message and per-chat size limits are gone; text extraction
  still stops at 25 MiB. Older stored files keep working.
- **Contract changes.** Browser chat sends need a `request_id`, so a retried send is
  answered once. New owner routes back everything above; `docs/*-openapi.json` lists them.
- The workflow builder's request is bounded to 1 MB in total, and plan readings and window
  labels are bounded and filtered.
- When the owner asks the companion in chat for something recurring, Pi offers a
  workflow draft in the Inbox and the companion mentions it. A narrow check of the owner's
  own message makes the offer before the model is called, so neither the model nor
  injected text can; the companion gets no new tool. Accepting drafts from that message as
  `from-chat` does. New admin routes: GET `/workflow-proposals/offers` and
  `/workflow-proposals/offers/{id}`, POST `/workflow-proposals/offers/{id}/dismiss`.
- Update Pillow to 13.0.0 for eight published advisories in 12.3.0. Pi decodes attached
  images with it.
- Update pypdf to 6.21.2 for eight published advisories in 6.19.0. Pi reads attached PDFs
  with it, in its isolated worker.
- A weekly security check (`.github/workflows/security.yml`) audits every dependency this
  repository ships against the public advisory databases each Monday and whenever they
  change, and fails loudly.

## 0.5.6

- Add a distinct ChatGPT subscription provider via Conker's private inference
  socket, using the official Codex managed device-code sign-in on the host.
  Keep OAuth credentials and refresh outside containers and the browser;
  Conker/Pi retains tools, permissions, streaming and stop authority.
- Add owner-authenticated, exact single-use verified subscription login, cancel,
  disconnect and catalogue operations. No API-key fallback or Codex agent runtime
  is introduced. Connection status is not proof of model entitlement.

## 0.5.5

- Add authenticated, operation-password-bound provider control through an optional
  private Conker host Unix socket. Credential inputs are write-only; strict status
  projections omit secrets, subprocess output and provider response bodies. Only
  named credential and paid-request-policy operations are admitted; no shell,
  Docker socket, arbitrary endpoint or environment editor is exposed.
- Require exact one-use password proofs for browser-session revocation and reject
  mismatched targets, request bodies and write query parameters.

## 0.5.4

- Preserve the router's bounded recent-exchange input scope in saved model-selection
  evidence, alongside current-request-only scope. Keep prompts, memory, credentials
  and unknown provider fields out of that public receipt; authority is unchanged.

## 0.5.3

- Apply saved Companion instructions to model context, matching custom agents.
  Keep the submitted profile revision frozen and leave legacy turns without an
  execution snapshot unchanged. Instructions do not grant tool or memory access.

## 0.5.2

- Support an explicit browser-cookie namespace for trusted instances sharing a
  hostname on different ports. Preserve the default cookie and all authentication,
  CSRF, proof, expiry and revocation controls; never fall back to another namespace.

## 0.5.1

- Add an explicit owner-authorized across-chat Companion memory scope. Existing
  profile defaults remain unchanged; custom agents and team roles cannot inherit
  this scope. Turn snapshots, private exclusions and namespace read keys still apply.
- Mark selected cancelled requests as historical, not pending work, when preparing
  subsequent answers. Preserve exact transcript text and context exclusions, and
  freeze the cancellation notice with the original turn context for truthful replay.
- Add regression coverage for cancellation context and excluded private requests.
  A model notice does not replace durable cancellation or reverse external actions.

## 0.5.0

- Publish the connected owner workspace: verified setup, projects, artifacts, agents,
  calls, schedules, host inventory and exact operation-bound browser controls.
- Publish staged, authenticated universal search with explicit opt-in semantic and
  ranking settings, source privacy checks and literal fallback.
- Include streamed and cancellable turns, source-bound memory forgetting,
  proactive proposals and pinned workflow capabilities.

- Load OpenRouter, OpenAI and Anthropic credentials once from fixed absolute regular files,
  reject ambiguous environment-plus-file configuration, and expose only configured provider
  identities through the secret-free health projection.

- Prevent newly admitted budget-held jobs from sorting behind the scheduler's fairness cursor.
  New holds are attempted once alongside the bounded rotating backlog, so cursor position cannot
  skip them while older held work still receives a fixed batch on every tick.

- Replace FastAPI's input-reflecting validation response with a static 422 body in Pi and
  Gateway. Rejected owner payloads can no longer echo an accidentally submitted credential.

- Add exact owner-browser routes for durable typed calls. Strict bounded projections expose
  call state, transcript events and retained request identities without audio, speech-provider,
  device, raw-media or credential fields. Start, update, interrupt, end and text turns preserve
  the existing call ledger and recovery rules; unrelated call and media routes remain admin-only.

- Promote configured file roots and durable directory receipts to exact owner-browser routes.
  Strict versioned projections expose names and entry kinds only; file contents, writes,
  recursive crawling, ToolGate action IDs and approval IDs remain outside the browser contract.

- Expose the canonical team definition lifecycle through exact owner-browser routes with
  bounded strict DTOs, active agent reference checks, compare-and-swap writes and immutable
  team revision history. Legacy stores retain their real current revision; restore revalidates
  selections. Configuration does not grant authority or expose preparation/execution routes.

- Add an exact owner-browser contract for read-only host inventory and configured target
  metadata. Strict versioned projections replace PIDs, daemon IDs and port identities with
  persistent keyed aliases; command lines, images, addresses, source paths, approvals and raw
  receipts remain hidden. Dependency failure stays unavailable, and no system mutation route
  is granted.

- Connect the canonical Companion character package to the owner browser contract. Current,
  immutable history and inert export are readable; save, import and restore require exact
  operation-bound verification and expected-revision checks. The 66-MiB character-only request
  envelope carries already bounded embedded media without raising ordinary browser limits or
  exposing other agent IDs.

- Add a schema-versioned owner-browser contract for existing schedules and redacted run
  history. Optimistic pause/enable, idempotent manual admission, cancellation, budget
  binding, approval resume and reconciliation use exact routes without returning target
  arguments, receipts or authority identifiers. Full schedule authoring remains recovery-only
  until Pi can validate arguments against authoritative ToolGate publication schemas.

- Add a schema-versioned, paginated owner-control contract for the durable artifact library.
  Conversation copies resolve exact completed assistant messages and authoritative privacy;
  responses carry explicit no-authority/content/execution markers, native exports stay inert,
  and binary downloads remain recovery-admin only because browser control is JSON-only.

- Add a schema-versioned, paginated owner-control contract for durable projects and canonical
  conversation/task/file links. Archive/restore preserves owner-visible tombstones; project
  removal, metadata search and caller-supplied privacy previews remain outside the browser
  allowlist. Responses include no source content or execution authority.

- Expose durable agent configuration through a schema-versioned, narrowly allowlisted owner
  control contract. Companion edits now append ordinary optimistic revisions while archive and
  delete remain impossible; runtime credentials and near-match browser paths are denied.

- Add one bounded, secret-free diagnostic contract shared by authenticated
  `GET /api/diagnostics` and the host-only `python -m gateway doctor` command.
  Findings have stable identities and identical recovery actions in the CLI and UI.

- Replace the impossible first-run setup ceiling with durable owner-attested evidence receipts for
  boundary review, protection verification and assembled rehearsal. Receipts are revisioned,
  restart-safe, expiry-aware and conflict-safe; recording one never executes or fabricates the
  external operation it references.

- Prevent Pi's internal-service and model-provider HTTP clients from inheriting ambient proxy
  routing, and keep redirects disabled at every runtime HTTP entry point.

- Add owner-only `GET /setup/status`, a schema-versioned read projection for the eight-step
  first-run workflow. States come from persisted Companion/model revisions and live dependency
  checks; typed prerequisites, reason codes, current step and one bounded next operation keep
  clients from inferring workflow semantics. Missing cross-service and host receipts remain
  visibly degraded or not started.

## 0.4.0

Memory, forgetting, browser auth, and the 2026-09-12 audit fixes.

- **Pi now remembers.** Committed conversation evidence queues to MemoryGate
  through a durable outbox under a stable id, retries cannot duplicate, and
  retrieval is tied to the turn that used it. Ingestion or retrieval failure is
  surfaced honestly rather than left to the model to mention.
- **Forgetting is real.** An owner-only offline command removes a session's
  messages and leaves content-free tombstones; citations to a deleted message
  read as tombstones, not dangling ids. Deletion is bound to the namespace that
  actually stored the evidence, so it works across an agent-id change.
- **Browser authority.** A gateway owns login and revocable server-side
  sessions with a Secure, HttpOnly, SameSite cookie, CSRF, expiry and logout.
  The owner-approval credential is never handed to the worker; an agent
  credential no longer implies approval authority.
- **Provenance on approvals** — the owner's originating words travel with the
  parked action, so an instruction injected into ingested content is visible as
  a mismatch.
- **Truthful money and status.** Unknown or incomplete model pricing counts as
  not-free and unknown-cost, never zero, and a per-request fee is honoured, so a
  paid model cannot slip through the free guard or be recorded at `$0`. A
  missing or non-affirmative tool outcome is never read as success. `/health`
  reports the model it must actually serve, not any installed model.
- **Honest recovery under concurrency.** Resume is serialized and compares
  state before writing, so a completed action is never overwritten as failed,
  and an interrupted-but-acted turn stays retrievable.
- **`action_id` on every dispatch**, matching ToolGate's durable-execution
  contract; a hosted-catalogue outage no longer removes the working local model.
- The image no longer carries build-context secrets, proven in CI.


- Persist ToolGate action IDs before dispatch and reuse them through approval and
  budget waits. Attach owner-created jobs on resume; uncertain outcomes use status
  checks without redispatch. Missing or negative receipts never imply success.
- Serialize resume claims and completion transitions. Recover acted turns after
  restart through the unreplied path, preserving the receipt and acted flag together.
- Require complete zero pricing for free hosted routing and preserve reported cost;
  missing cost stays unknown. Keep local fallback during catalogue outages and keep
  model summaries at assistant trust.
- Pin memory delivery namespaces before sending; validate namespaced receipts and
  retain the original destination for forgetting. Expose permanent delivery failures
  and host-only repair. Limit new user messages to 16,000 characters.
- Add the September audit mutation drill; see `docs/AUDIT_RECOVERY.md` for upgrade
  behavior, legacy holds, owner spending jobs and memory recovery commands.

- Add a separate HTTPS browser gateway with host-managed password setup/recovery,
  durable revocable sessions, CSRF protection, expiry and login attempt limits.
  Pi accepts a distinct runtime key whose hash is deployed to the worker; owner
  approval credentials remain exclusively in the gateway. ToolGate's new owner
  endpoint contract remains a separate integration dependency.
- Preserve memory notices across the browser proxy, refuse unlisted operations
  and redirects, and provide live HTTPS and mutation drills. Gateway TLS identity
  persists across restarts and renews only through an explicit host command.

- Commit user evidence and its delivery outbox atomically; retry stable IDs without
  duplicate MemoryGate records. Serialize concurrent message sequence allocation.
- Retrieve and retain exact per-turn memory context; expose pending ingestion and
  retrieval gaps in turn/session responses and authenticated `GET /memory`.
- Propagate offline forgetting as durable deletions, clear cached contexts and
  suppress retrieval until MemoryGate acknowledges deletion.

- Add offline, owner-operated forgetting of sessions and their fork descendants,
  including summaries and approval provenance held by Pi. Runtime history stays
  append-only; immutable content-free receipts and message envelopes survive.
- Add authenticated `GET /messages/{id}` for evidence citations. Forgotten sources
  return explicit tombstones with deletion time and receipt ID. Sessions expose
  the same tombstones and a new `forgotten` status; their turns cannot resume.
- Refuse deletion while Pi is running and refuse startup after interrupted
  database cleanup until the owner retries. Preserve execution outcomes and
  accounting without retaining conversation payloads.

## 0.3.0

The action boundary, and the approval round trip.

- **Pi can act, and only through ToolGate.** Pi executes nothing itself. It is
  given a *scoped* execution key and asks ToolGate what that key may reach on
  every turn rather than caching it, because the owner can change scope at any
  moment and a cached list would let Pi offer a tool it no longer has.
- **A tool call is a line of JSON the loop owns**, not a provider's native
  tool-calling schema. Pi routes across local, free hosted and paid models and
  their formats disagree; a format the loop owns behaves identically everywhere.
  The parser is anchored to a whole line, so prose *about* a tool call is not
  acted on as one.
- **A tool outside the key's scope is never forwarded.** ToolGate would refuse
  it, but forwarding would put an unscoped tool id in its audit trail on Pi's
  authority, which is not Pi's to spend.
- **A gated tool parks the turn** as `awaiting_approval` with the action stored
  whole, rather than failing it. The owner has not said no; they have not been
  asked yet. A restart does not withdraw the question, and `GET /approvals` is
  one queue across all sessions — an approval nobody sees is an action that
  silently never happens.
- **Resuming replays the stored action**, never one rebuilt from the
  conversation, so an approval cannot be spent on a different action than the
  one the owner was shown. A stale approval re-parks the turn on the *new*
  request instead of leaving it behind a dead nonce it could never clear.
- **An action that happened is never recorded as one that did not.** A tool can
  succeed and the model can then fail to say so. Those turns are recorded as
  `acted_no_reply`, not `failed`, and resuming asks only for the missing reply
  without running the action a second time. Neither route answers with an error
  status in that case: an error code invites a retry, and retrying would do the
  thing twice. `GET /turns/unreplied` lists them. Found by a live round trip
  against ToolGate, where the tool ran, the local model timed out afterwards,
  and the turn claimed the action had failed.
- **Timeouts are configuration, and generous locally.** `PI_LOCAL_TIMEOUT_S`
  defaults to 600s. The previous fixed 120s ceiling fired on a local model that
  was still thinking and turned *slow* into *failed*.
- `GET /tools`, `GET /approvals`, `GET /turns/unreplied` and
  `POST /turns/{id}/resume` are new; `/health` gains an `action_boundary` check.
  Turns gain `acted`. A database created by an earlier version is migrated in
  place.
- **`/health` reported `0.1.0` while the module was at `0.2.0`.** Fixed, and it
  is the same class of defect as the one above: a status that is quietly wrong.

## 0.2.0

Provider adapters and routing.

- **OpenRouter adapter with discovered models.** A hardcoded list is stale
  within weeks; OpenRouter carried 431 models and 22 free ones the day this was
  written, and both numbers moved by the next run.
- **Free by default, enforced in code.** Paid models are refused unless
  `PI_ALLOW_PAID_MODELS` is set, a model outside the catalogue is refused rather
  than called blind, and an unreadable price counts as *not free* - treating it
  as zero is the assumption that produces a bill.
- **Only text-in, text-out models are routed to.** Some zero-priced entries are
  audio or image generators, and a conversation routed into one fails in a way
  that is very hard to read from the answer.
- **Escalation on explicit signals**, each recorded on the turn with its reason.
  The router does not read the message: a classifier nobody evaluates deciding
  what every turn costs is worse than a crude rule that can be measured.
- **Candidate fallthrough.** Listed and priced at zero does not mean callable -
  some free models are gated to particular clients and answer 403. Pi tries the
  next candidate and records what it skipped, so a model that always refuses is
  visible rather than showing only as latency. A bad key or exhausted quota is
  deliberately not treated this way; those fail on every candidate alike.
- `GET /models` reports what Pi can route to, and `turns` gain `route_tier` and
  `route_reason`. A database created by 0.1.0 is migrated in place.

## 0.1.0

First release: sessions and the turn loop.

- **Append-only history, enforced by the database.** No function updates or
  deletes a message and the schema refuses both with triggers, so a caller
  reaching past the API still cannot rewrite what was said.
- **Sessions survive restart**, and a turn that was running when the process
  stopped is marked `interrupted` with its reason rather than vanishing.
- **Forking instead of truncation** when history outgrows the window: the
  session closes with a summary and a child opens seeded by it, pointing back
  at the parent. If the summary itself cannot be written the fork still happens
  and says so, rather than leaving a child claiming context it does not have.
- **A failed turn keeps the message that was sent.** It was said.
- Turns record provider, model, tokens, cached tokens, cost and latency. A
  provider that reports no price yields `null`, never `0`.
- `GET /health` in the module contract shape, probing the store and the provider.
- Refuses to start without an admin key of at least 16 characters.
- One provider adapter, Ollama, so the loop can be exercised end to end at no
  cost. Routing and the rest of the adapters are #28.
