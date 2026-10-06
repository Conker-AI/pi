# Changelog

Versions are the module's own, not an API revision. A change to the shape of any
endpoint is a contract change and gets its own entry — replacing a module has to
be a decision with visible consequences.

## Unreleased

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
