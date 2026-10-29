# Changelog

Versions are the module's own, not an API revision. A change to the shape of any
endpoint is a contract change and gets its own entry — replacing a module has to
be a decision with visible consequences.

## Unreleased

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
- **MemoryGate and ToolGate use the owner's model roles.** `POST /inference` (service key in
  `X-Pi-Service-Key`, hashes in `PI_SERVICE_KEYS_SHA256`) answers one prompt for the calling
  service's own role, `memory-sorting` or `tool-text`, through Pi's normal role call. Until the owner
  picks a model for one, it uses the local model: nothing leaves the server on upgrade. Each call's
  usage is recorded without its text; `GET /inference/usage` lists it. See docs/inference.md.
- A model role may wait up to 10 minutes (was 2). Saved settings keep their wait; when nothing was
  saved, a role whose model is local waits up to 5 minutes, and background thinking honors its
  role's wait instead of stopping at 2 minutes.

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
