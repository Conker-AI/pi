# Workflow drafts

The owner describes a workflow in words and the talking model drafts it for ToolGate's visual
editor. Pi only drafts. ToolGate stores and validates the draft, and only the owner publishes it.
ToolGate dropped its own AI builder in September (ToolGate's `docs/AI_RETIREMENT.md`); this is its
replacement, on Pi's side of the line.

## The route

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
- Output that is not a usable draft is refused with `502 unusable`. A missing model configuration
  is `503 not_configured`, and a model that does not answer is `503 unavailable`.

The draft confers nothing. Saving it in ToolGate confers nothing either. Publishing asks the owner
for an authorization (`owner_confirmation` by default), and every tool it calls keeps its own
grants and approvals.

The command line drives the whole loop: `conker workflow draft "..."` in Conker.
