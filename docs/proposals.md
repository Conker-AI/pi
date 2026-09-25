# Proposals

The proposal engine notices what the owner keeps doing and offers to take it on.
A **pass** reads the owner's own messages since its watermark, asks the model assigned
to the `proposals` role what looks repeated, forgotten or automatable, and records at
most three proposals, each tied to the messages that prompted it.

Nothing executes. Accepting records a decision; any later action still goes through
ToolGate and its approvals.

## Turning it on

1. Assign a model to the **Proposals** role (model roles). Upgrades add the role
   disabled, so conversations are never analyzed until the owner chooses a model.
2. Set `PI_PROPOSALS_ENABLED=true`. The worker checks hourly and runs a pass at most
   once per `PI_PROPOSALS_INTERVAL_HOURS` (default 24). A pass denied by quiet hours or
   the daily budget is retried at the next hourly check.
3. The owner can also run a pass now: `POST /proposals/passes` (owner credential;
   through the browser this is an owner-control write and needs the password).

## What a pass reads

Only user messages that are outside incognito (memory-disabled) and harness-disabled
chats and not in forgotten chats: at most 200 messages and 24,000 characters (1,000
per message). The watermark advances only after a completed pass, so a failed pass
rereads the same messages.

## Limits

- Fewer than 3 new messages: `skipped`, nothing spent.
- The daily suggestion budget and quiet hours are checked with the proactive
  reservation ledger before any model call: `denied`, nothing spent.
- The reservation records suggestions, not money. A hosted model's cost is not yet
  capped by this ledger; choose a local or budgeted model for this role.

## Output rules

Proposals must cite the aliases of the supplied messages. Invented or missing evidence
drops the proposal; a proposal that cannot say why it is shown is not shown. Titles are
fingerprinted: an open, "never" or recently declined idea is not proposed again and
does not take a slot from a new one.

## Decisions

`POST /proposals/{id}/decision` with `accept`, `decline` or `never`. A decision is
final; repeating the same decision is a no-op. `decline` suppresses the idea for 30
days, `never` permanently. Declined titles are shown to the model as ideas to avoid.
Browser decisions are session-bound writes (Conker ADR-0010): they grant nothing.

## API

| Route | Credential | Purpose |
|---|---|---|
| `GET /proposals?state=open` | runtime | List with evidence excerpts (forgotten sources are marked unavailable) |
| `POST /proposals/{id}/decision` | runtime | Accept, decline or never |
| `GET /proposals/passes` | runtime | Recent passes and the watermark, without content |
| `POST /proposals/passes` | owner | Run a pass now |

Proposals and passes are retained; proposal content cannot be edited after creation.
