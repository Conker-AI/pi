# Change requests: Conker prepares, the owner verifies (2026-10-25)

> Since 2026-10-26 Conker's own tools follow the access layers (docs/access.md). Since 2026-10-29
> only remembering is Keep by default (done at once, still recorded as an approved request, listed
> in Handled); scheduling waits for the owner unless his settings for an agent or a chat loosen
> it; a tool draft always waits, as below. See "What Conker makes on its own" at the end.

Conker can prepare anything the owner could make by hand, but never make it real on its own.
Three of its own tools (`pi/turns/native_tools.py`) are offered to the companion next to the owner's
published tools, and to other agents when selected for them, whenever ToolGate is set up and the
owner did not leave tools out at setup:

| Tool | What it files | What approving does |
|---|---|---|
| `conker.schedule` | an AI job: name, instructions, repeat (hours, daily, weekly, webhook) | creates the AI job (`agent_jobs.create`) in the owner's time zone |
| `conker.draft_tool` | a tool request, drafted in the background from the tools Conker can use (see below) | once it is ready, the owner's browser saves it in ToolGate's editor and reports the draft id; the owner publishes it there |
| `conker.remember` | one stated fact | stores the exact text through MemoryGate's correction capability (`PUT /runtime/corrections/remember/{request id}`), so a retry never stores it twice; if memory is not connected or does not answer, the request stays open (503) |

Calling one records a change request (`pi/agents/change_requests.py`) and answers at once with
"waiting for the owner" (a tool request: "drafting"), unless his access settings let it go ahead
(Keep). A request is never edited after it is filed:
what the owner approves is exactly what Conker asked for. The one exception is a tool request,
whose draft Pi fills in after filing. Bad arguments are an answer to the model, not a request.

Nothing here acts outside Pi's own store and memory (ADR-0005), and Pi holds no ToolGate authority:
tool drafts reach ToolGate only through the owner's browser. With the default settings a prompt
attack on Conker can therefore at worst leave a request the owner declines, or a memory stored as
Conker's own (below). Conker's own tools do not count as tool use for model routing.

## Tool requests (2026-10-29)

When the owner asks for a new tool, Conker calls `conker.draft_tool` and the chat answers at once:
it is drafting, and the draft will be in the Inbox. No model call waits in the turn, and the
reviewer is not asked (the request makes nothing; it only fills a card). The same request is made
without a chat by `POST /tool-drafts` (the Tools screen's "Describe a new tool", and the command
line). One drafter serves all three (`pi/agents/tool_drafts.py`, worker `pi/turns/tool_drafting.py`).

The request's `state` stays `open` throughout; its payload says how far drafting got:

| `payload.status` | payload | card |
|---|---|---|
| `drafting` | `{"description", "status"}` | Drafting... |
| `ready` | `{"description", "status", "document", "issues", "steps", "checked"}`, plus `"note"` when `checked` is false | Ready to review |
| `failed` | `{"description", "status", "error"}` | Couldn't draft: `error` |

A request filed before this has no `status` and counts as `ready`. `steps` are plain-word lines,
one per step in the order they run ("Search the web for the topic (uses Web search)"), which Pi
reads off the document itself; no model writes them. While drafting the title is "Draft a tool:
<description>"; when ready it is "Add the tool “<name>”" and the summary is what the drafter says
the tool does. `saved` and `approve` refuse a request that is not ready (409 `not_ready`).

How it drafts, in the background, one request at a time:

1. The tools Conker can use, as ToolGate lists them for its key, published workflows included as
   building blocks; Conker's own tools are left out. Only those that share words with the
   description reach the model: at most 8, words few tools have counting more, each described in a
   few words (when none match, all are sent).
2. The `drafting` model role drafts, or the answer role until the owner picks a drafting model
   (Settings > Models). Each model call gets its role's own time limit, gives way to a chat reply on
   a shared local model and then goes on, and is cancelled when Pi stops.
3. ToolGate's read-only check (`POST /v2/editor/check`: shape, graph rules, every called tool and
   workflow exists) reads the document; the model repairs it at most twice from what it found.
   When ToolGate does not answer, or is older and lacks the check (404), the draft is ready with
   Pi's own issues, `checked: false` and a note that Tools checks it again when opened.
4. A question from the drafter ("which chat?") fails the card with its words, so the owner can
   change the description and try again. A request still drafting when Pi stopped is failed when
   Pi starts: "Pi restarted while drafting. Try again."

Routes, all owner (`require_owner`; the browser's signed-in session, no password: they make
nothing real):

- `POST /tool-drafts` `{"description": str 1..4000, "request_id": str 16..128 [A-Za-z0-9_.:-]}`:
  the new request (its view, as `GET /change-requests/{id}`), drafting. The same `request_id`
  gives back the same request; with another description it is 409 `request_reused`.
- `POST /change-requests/{id}/retry` with an empty body or `{"description": str 1..4000}`: back to
  drafting, from the new description if given. 409 `drafting` while it drafts, `decided` once it
  is no longer open, `not_a_tool_draft` for other requests.
- `POST /change-requests/{id}/published` `{"toolId": str, "name": str 1..160}`: the owner
  published the tool (`toolId` is the id ToolGate's publish answer gives, the one Conker sees).
  Recorded as `result.published = {"toolId", "name", "publishedAt"}` and the request counts as
  approved. The first time, if the request came from a chat, Conker's message is stored there (no
  model call): "“<name>” is live. Want me to run it now?" It goes to the chat the conversation
  continues in after a fork, waits while the chat is answering, and is not repeated.

Other owner routes: `GET /change-requests`, `GET /change-requests/{id}`, `POST .../approve` (password
verified; jobs and memories), `POST .../saved` with `{"draftId": ...}` (tool requests only; the
owner's password-verified save in Tools already happened, so this records the id and needs only the
signed-in session), `POST .../decline` (signed-in session only).
`GET /tools` lists Conker's own tools first, so the owner sees everything the AI can call. The Inbox
lists open requests (`kind: "change-request"`). Forgetting a chat deletes its requests.

## What Conker makes on its own (2026-10-29)

- **Private chats:** a chat with memory off (or inheriting it from messages it carries, or with a
  message said while it was off) is never offered `conker.remember` or `conker.schedule`, and a
  call is refused (`status: "refused"`). A job the owner approves from a request filed in a chat
  that is private by then gets a private chat of its own (`privacy` in the job's definition),
  kept for every replacement chat.
- **Jobs:** each job records `createdBy`: `"owner"` (made by hand, or by his approval here) or
  `"conker"` (made at once where his settings let it). A job Conker made on its own reserves one
  suggestion from his daily proactive budget before each run (quiet hours, his proactivity switch
  and the daily limit hold it back, with no model call), and its instructions never count as his
  own words. No run of any AI job is offered `conker.schedule` or `conker.draft_tool`. Conker's
  requests make no job while 20 AI jobs are on (refused, or 409 `job_limit` on approval), and
  Conker makes at most 3 a day on its own; a fourth waits for his approval.
- **Memories:** a Keep memory is stored with `"origin": "conker"` (MemoryGate keeps it as an
  assistant statement, medium confidence) under the key `<request id>-conker`; his approval
  stores his words as his (`origin` left out) under the request id, so the two never collide.
- **Links:** every Keep result (the AI job, or a memory MemoryGate stored new) is recorded apart
  from the request, with its chat (`keep_results`, ids only). Forgetting the chat turns those jobs
  off (marked `turnedOff: "chat_forgotten"`, never deleted) and has Pi forget those memories once
  it runs again (docs/forgetting.md).
- **Limits:** at most 3 of Conker's requests wait per chat and 20 in all; past that a call answers
  `status: "refused"` with a sentence Conker passes on. The owner's own requests (`/tool-drafts`)
  are not limited.

CLI parity: `conker inspect change-requests` lists open requests; `conker apply requests FILE`
takes `{"operation": "approve" | "decline", "id": "creq_..."}`,
`{"operation": "saved", "id": "creq_...", "draftId": "..."}` after saving the document with
`conker apply tools`, `{"operation": "retry", "id": "creq_...", "description"?: "..."}`, and
`{"operation": "published", "id": "creq_...", "toolId": "...", "name": "..."}`. Publishing with
`conker apply tools` takes `"conkerMayUse": true` to let Conker use the tool.

By hand: the owner adds a memory with `POST /memory/remember` (`{"request_id", "text"}`, password
verified in the browser; `conker apply memory` with `"operation": "remember"`), which uses the same
MemoryGate route, so a memory added by hand and one approved from a request are stored the same way.
