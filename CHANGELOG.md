# Changelog

Versions are the module's own, not an API revision. A change to the shape of any
endpoint is a contract change and gets its own entry — replacing a module has to
be a decision with visible consequences.

## Unreleased

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
