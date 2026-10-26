# Change requests: Conker prepares, the owner verifies (2026-10-25)

Conker can prepare anything the owner could make by hand, but never make it real on its own.
Three of its own tools (`pi/turns/native_tools.py`) are offered to the companion next to the owner's
published tools, and to other agents when selected for them, whenever ToolGate is set up and the
owner did not leave tools out at setup:

| Tool | What it files | What approving does |
|---|---|---|
| `conker.schedule` | an AI job: name, instructions, repeat (hours, daily, weekly, webhook) | creates the AI job (`agent_jobs.create`) in the owner's time zone |
| `conker.draft_tool` | a workflow drafted by the drafting model from the owner's existing tools | the owner's browser saves it in ToolGate's editor and reports the draft id; the owner publishes it there |
| `conker.remember` | one stated fact | stores the exact text through MemoryGate's correction capability (`PUT /runtime/corrections/remember/{request id}`), so a retry never stores it twice; if memory is not connected or does not answer, the request stays open (503) |

Calling one only records a change request (`pi/agents/change_requests.py`) and answers at once with
"waiting for the owner". A request is never edited after it is filed: what the owner approves is
exactly what Conker asked for. Bad arguments are an answer to the model, not a request. A draft
that needs more detail returns the builder's question instead of filing.

Nothing here acts outside Pi's own store (ADR-0005), and Pi holds no ToolGate authority: tool drafts
reach ToolGate only through the owner's browser. A prompt attack on Conker can therefore at worst
leave a request the owner declines. Conker's own tools do not count as tool use for model routing.

Owner routes: `GET /change-requests`, `GET /change-requests/{id}`, `POST .../approve` (password
verified; jobs and memories), `POST .../saved` with `{"draftId": ...}` (tool requests only; the
owner's password-verified save in Tools already happened, so this records the id and needs only the
signed-in session), `POST .../decline` (signed-in session only).
`GET /tools` lists Conker's own tools first, so the owner sees everything the AI can call. The Inbox
lists open requests (`kind: "change-request"`). Forgetting a chat deletes its requests.

CLI parity: `conker inspect change-requests` lists open requests; `conker apply requests FILE`
takes `{"operation": "approve" | "decline", "id": "creq_..."}`, or
`{"operation": "saved", "id": "creq_...", "draftId": "..."}` after saving the document with
`conker apply tools`.

By hand: the owner adds a memory with `POST /memory/remember` (`{"request_id", "text"}`, password
verified in the browser; `conker apply memory` with `"operation": "remember"`), which uses the same
MemoryGate route, so a memory added by hand and one approved from a request are stored the same way.
