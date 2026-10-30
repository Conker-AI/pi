# Workflow drafts

The owner describes a workflow in words and the drafting model drafts it for ToolGate's visual
editor. Pi only drafts. ToolGate stores and validates the draft, and only the owner publishes it.
ToolGate dropped its own AI builder in September (ToolGate's `docs/AI_RETIREMENT.md`); this is its
replacement, on Pi's side of the line.

## Tool requests: the one drafter (2026-10-29)

Conker drafts tools itself. In chat it calls `conker.draft_tool` whenever the owner asks for a new
tool; the Tools screen and the command line (`conker tool draft`, and `conker workflow draft`,
which now does the same) call `POST /tool-drafts`. Each files a `tool_draft` change request at
once and Pi drafts it in the background: only the capabilities that match the description (at
most 8, published workflows included), the `drafting` role with its own time limits, ToolGate's
read-only document check and up to two repairs, then a card in the Inbox with the steps in plain
words. When the drafter needs a detail, a request from a chat asks there and the owner's answer
goes on with the same draft (`needs_detail`). The whole flow, its payload shapes and its routes
are in [change requests](change-requests.md#tool-requests-2026-10-29).

Publishing in Tools with "Let Conker use this tool" (`conkerMayUse: true` on the publish route,
password verified) shows the tool to agents and grants Conker's key in the same step; the browser
then reports `POST /change-requests/{id}/published` and Conker says in the chat that asked that the
tool is live.

## What the drafter is given

Each model call gets one JSON message (`pi/agents/workflow_drafts.py`, `Request`):

| Field | Meaning |
|---|---|
| `description` | What the workflow should do, in the owner's words (up to 4,000 characters). |
| `capabilities` | The tools and published workflows the draft may use: `id`, `kind` (`tool` or `workflow`), `name`, `description`, `inputs`, `version`. Up to 200. |
| `conversation` | The drafter's earlier questions and the owner's answers, oldest first. Up to 16. |
| `draftId` | The draft's id while it is repaired; otherwise one is made from its name. |
| `previous`, `issues` | A draft to repair and what is wrong with it, from Pi or from ToolGate's check. |

It answers `{reply, ready, document}`. With `ready: false`, `reply` is one question, or says why
the request cannot be drafted with the listed capabilities. With `ready: true`, `reply` says what
the workflow does and `document` is an editor draft for `POST /v2/owner/editor-drafts/{id}` in
ToolGate; steps that name a tool or workflow missing from `capabilities` come back as issues for
a repair.

## What the model can and cannot decide

- It sees the request and nothing else: no memories, chats or credentials. The conversation it
  gets is only its own questions and the owner's answers to them.
- Pi sets the parts that carry authority or layout. `kind` is always `workflow`, `credentialRefs`
  is always empty (ToolGate refuses them on publish anyway) and `agentVisible` is false until the
  owner turns it on (publishing with "Let Conker use this tool" does). Each `workflow_call` gets
  the version listed in `capabilities`. Steps are laid out top to bottom. Budgets outside the
  editor's bounds fall back to 50 steps, 20 loop items and 15 seconds.
- An unknown `effect` becomes `write`, the most careful label.
- The whole request is at most 1 MB. Terminal control characters are removed from `reply`.
- The `drafting` model role drafts when the owner enabled it with a model; otherwise the
  talking model (the answer role) does. Each call gets the role's own time limit.
- Output that is not a usable draft gets one more try with the error shown; a second unusable
  reply fails the card. A missing model configuration or a model that does not answer fails the
  card in plain words.

The draft confers nothing. Saving it in ToolGate confers nothing either. Publishing asks the owner
for an authorization (`owner_confirmation` by default), and every tool it calls keeps its own
grants and approvals.

## Retired (2026-10-30)

Three older ways in are gone, replaced by tool requests:

- `POST /workflow-drafts/draft`, the command line's route that drafted while it waited (up to 10
  minutes per model call). The command line now files a tool request.
- `/workflow-proposals`, which linked a draft the command line made from a chat message to that
  message, with "Workflow draft ready" items (`kind: "workflow"`) in the Inbox.
- `/workflow-proposals/offers`, Pi's offers to draft a workflow from a message
  (`kind: "workflow-offer"` items), which Pi stopped making on 2026-10-29.

Their tables (`workflow_proposals`, `workflow_offers`) stay, because dropping them would need a
migration: nothing writes or lists them any more, and forgetting a chat still deletes its rows
there (`pi/agents/retired_workflows.py`; the audit mutation drill checks it). Drafts saved in
ToolGate from them stay in Tools, as the owner's own work.
