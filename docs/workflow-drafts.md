# Workflow drafts

The owner describes a workflow in words and the drafting model drafts it for ToolGate's visual
editor. Pi only drafts. ToolGate stores and validates the draft, and only the owner publishes it.
ToolGate dropped its own AI builder in September (ToolGate's `docs/AI_RETIREMENT.md`); this is its
replacement, on Pi's side of the line.

## Tool requests: the one drafter (2026-10-29)

Conker drafts tools itself. In chat it calls `conker.draft_tool` whenever the owner asks for a new
tool; the Tools screen and the command line call `POST /tool-drafts`. Each files a `tool_draft`
change request at once and Pi drafts it in the background: only the capabilities that match the
description (at most 8, published workflows included), the `drafting` role with its own time
limits, ToolGate's read-only document check and up to two repairs, then a card in the Inbox with
the steps in plain words. The whole flow, its payload shapes and its routes are in
[change requests](change-requests.md#tool-requests-2026-10-29).

Publishing in Tools with "Let Conker use this tool" (`conkerMayUse: true` on the publish route,
password verified) shows the tool to agents and grants Conker's key in the same step; the browser
then reports `POST /change-requests/{id}/published` and Conker says in the chat that asked that the
tool is live.

## The command line's route

The command line's older way in, until it moves to `POST /tool-drafts`. It drafts one document
from the capabilities the caller sends and waits for the answer.

POST `/workflow-drafts/draft` (admin key) with:

| Field | Meaning |
|---|---|
| `description` | What the workflow should do, in the owner's words (up to 4,000 characters). |
| `capabilities` | The tools and published workflows the draft may use, as listed by ToolGate's owner editor catalogue: `id`, `kind` (`tool` or `workflow`), `name`, `description`, `inputs`, `version`. Up to 200. |
| `conversation` | Earlier questions from the builder and the owner's answers, oldest first. Up to 16. |
| `draftId` | Optional id for the draft; otherwise one is made from its name. |
| `previous`, `issues` | A draft to repair and what is wrong with it, from Pi or from ToolGate's validation. |

The answer is `{reply, ready, document, issues, modelId, configurationRevision}`:

- `ready: false`: `reply` is one question, or says why the request cannot be drafted with the listed
  capabilities. `document` is null.
- `ready: true`: `reply` says what the workflow does, and `document` is an editor draft for
  `POST /v2/owner/editor-drafts/{id}` in ToolGate. `issues` lists steps that name a tool or workflow
  missing from `capabilities`. Send them back with `previous` to get a fix.

## What the model can and cannot decide

- It sees the request and nothing else: no memories, chats or credentials.
- Pi sets the parts that carry authority or layout. `kind` is always `workflow`, `credentialRefs`
  is always empty (ToolGate refuses them on publish anyway) and `agentVisible` is false until the
  owner turns it on. Each `workflow_call` gets the version listed in `capabilities`. Steps are laid
  out top to bottom. Budgets outside the editor's bounds fall back to 50 steps, 20 loop items and
  15 seconds.
- An unknown `effect` becomes `write`, the most careful label.
- The whole request is at most 1 MB. Terminal control characters are removed from `reply`,
  which the command line shows as a question.
- The `drafting` model role drafts when the owner enabled it with a model; otherwise the
  talking model (the answer role) does.
- On this route the model gets up to 10 minutes per call, longer than a chat answer's limit:
  writing a whole document on a CPU-only local model takes minutes and the command line waits.
  Tool requests drafted in the background use the role's own limit instead.
- Output that is not a usable draft is refused with `502 unusable`. A missing model configuration
  is `503 not_configured`, and a model that does not answer is `503 unavailable`.

The draft confers nothing. Saving it in ToolGate confers nothing either. Publishing asks the owner
for an authorization (`owner_confirmation` by default), and every tool it calls keeps its own
grants and approvals.

The command line drives the whole loop: `conker workflow draft "..."` in Conker. The capability
selection it did (matching words, rarer words counting more) now lives in Pi
(`pi/agents/draft_capabilities.py`), shared with tool requests.

## Drafts made from a chat message

The owner can turn one of their own chat messages into a workflow (`conker workflow from-chat`
now, a button with the UI). The command line drafts it as above, saves it in ToolGate's editor,
then records the link here with POST `/workflow-proposals` (admin key):
`{sessionId, messageId, draftId, name, valid}`. Pi refuses a message that is not the owner's, is
not in that chat, or was forgotten.

- Each open record is a **Needs you** item in the [Inbox](inbox.md), `kind: "workflow"`:
  "Workflow draft ready: <name>", or "Workflow draft to fix: <name>" when ToolGate found problems.
- POST `/workflow-proposals/published` `{draftId}` closes the items for a published draft;
  POST `/workflow-proposals/{id}/dismiss` closes one without publishing. GET `/workflow-proposals`
  lists the open ones.

## Offers made in chat (retired)

Pi used to put an offer in the Inbox when the owner asked in chat for something recurring, and the
companion was told it could not draft anything itself. Conker now drafts tools itself
(`conker.draft_tool`), so Pi makes no new offers and the companion is no longer told so. Offers
already made stay until the owner accepts or dismisses them:

- Each open offer is a **Needs you** item, `kind: "workflow-offer"`: "Draft a workflow from your
  message? <its first 80 characters>".
- Accepting is drafting from that message exactly as above (`conker workflow accept wfo_...`
  runs `from-chat`); recording that draft closes the offer. POST
  `/workflow-proposals/offers/{id}/dismiss` declines it. GET `/workflow-proposals/offers` and
  `/workflow-proposals/offers/{id}` read them (admin key).
- Pi keeps only the link and its state, and forgets it with the chat.
- Pi keeps only the link and the draft's name. Forgetting the chat deletes the record (the audit
  mutation drill checks it); the ToolGate draft stays, as the owner's own work.

Still to retire, once the command line drafts through `POST /tool-drafts`: `/workflow-drafts/draft`,
the `/workflow-proposals` routes (`conker workflow from-chat`, `accept`, `dismiss` and `publish`
call them) and their Inbox items, and then the `workflow_offers` and `workflow_proposals` tables.
