# Pi documentation

One page per capability. Each says what is built, what it guarantees, and what it does not do yet.
What works end to end in a real deployment is tracked in Conker's
[status page](https://github.com/Conker-AI/conker/blob/main/docs/status.md).

## Core

| | |
|---|---|
| [Turns: acting, recording and status](turns.md) | Approvals, `acted_no_reply`, interrupted turns, status words |
| [Model routing and roles](routing.md) | Which model answers, why, and the helper roles |
| [Model catalogue and roles](model-roles.md) | Role assignments and adapters |
| [Model calls for services](inference.md) | MemoryGate and ToolGate use the owner's model roles |
| [Model profiles](model-profiles.md) | Read-only statistics from recorded outcomes |
| [Auto settings](auto-settings.md) | Proposed settings for the next message in Auto mode |
| [First-run setup status](setup-status.md) | Versioned owner-only setup evidence |
| [Direct text providers](direct-providers.md) | Hosted providers without a router |
| [Browser authentication](browser-auth.md) | Gateway login, sessions, recovery |
| [Turn submissions](turn-submissions.md) | Durable sends and exact message provenance |

## Conversations

| | |
|---|---|
| [Turn controls](turn-control.md) · [Steering](turn-steering.md) · [Queue](turn-queue.md) | Stop, steer and queue turns |
| [Response versions](response-versions.md) · [Retries](response-retries.md) · [Message forks](message-forks.md) · [Judgements](response-judgements.md) · [Fact checks](fact-checks.md) | Regenerate, branch, judge and fact-check |
| [Drafts](drafts.md) · [Search](conversation-search.md) | Unsent text and finding past chats |
| [Attachments](attachments.md) · [In turns](attachment-turns.md) · [Document text](document-text.md) | Files in conversation |
| [Session settings](session-settings.md) · [Output settings](output-settings.md) | Per-chat privacy, agent and output selections |
| [Chat names](chat-names.md) | Owner rename and Jev's automatic name |
| [Prepared context](turn-context.md) · [Context controls](context-controls.md) · [Context selection](context-retrieval.md) · [Summaries](context-summaries.md) | What the model is given |
| [Decisions and open threads](decisions.md) | What was decided and what is still open |

## Memory and forgetting

| | |
|---|---|
| [Conversation memory](memory.md) | The outbox to MemoryGate, and visible gaps |
| [Forgetting a conversation](forgetting.md) · [Forgetting a memory](memory-forget.md) | Removal with receipts |
| [Importing from ChatGPT and Claude](imports.md) | Exports kept as read-only chats; your words offered to memory as outside content |
| [Memory corrections](memory-proposals.md) · [Specialist memory](specialist-memory.md) | Reviewed changes and scoped reads |

## Doing things

| | |
|---|---|
| [Scheduled jobs](jobs.md) | Pinned tool and automation runs on a schedule |
| [Workflow drafts](workflow-drafts.md) | The talking model drafts an editor workflow from the owner's words |
| [Proposals](proposals.md) · [Proactive budgets](proactive-budgets.md) · [Recurring budgets](recurring-budgets.md) | Ideas Conker offers, and their limits |
| [Tasks, runs and events](task-ledger.md) | The durable work ledger |
| [Research](research.md) | Research request contract |
| [Projects](projects.md) · [Sources](project-sources.md) · [Context](project-context.md) | Owner projects in turns |
| [Artifacts](artifacts.md) | Durable outputs |
| [Agents](agents.md) · [Collaboration](collaboration.md) · [Team execution](team-execution.md) · [Characters](characters.md) | Configurable agents and teams |
| [Access: chat, agent, tool](access.md) · [Change requests](change-requests.md) | Who decides each action, and Conker's requests the owner verifies |
| [Owner preferences](owner-preferences.md) · [Owner settings](owner-settings.md) · [Continuity](continuity.md) · [Notifications](continuity-notifications.md) · [Inbox](inbox.md) · [Inbox answers](inbox-answers.md) | How Conker follows up and what needs the owner |
| [Plan usage alerts](plan-alerts.md) | A heads-up when a ChatGPT or Claude plan passes the owner's level, and when it resets |
| [Calls](calls.md) · [Speech adapters](speech-adapters.md) | Voice |

## The machine

| | |
|---|---|
| [System inventory](system-inventory.md) · [System actions](system-actions.md) · [Port changes](system-port-actions.md) | Observing and changing the host through ToolGate |
| [Directory reads](filesystem-reads.md) · [Owner terminal](owner-terminal.md) | Files and the owner-only terminal |

## Recovery

| | |
|---|---|
| [Recovery drill](recovery-drill.md) · [Replaying deletions](recovery-deletions.md) | Restoring Pi and keeping forgotten data forgotten |
| [Model evaluations](model-evaluations.md) | Saved helper-model evaluations |
| [Audit repair, 2026-09-12](AUDIT_RECOVERY.md) | A historical repair record |

## API reference

[Pi OpenAPI](pi-openapi.json) · [Gateway OpenAPI](gateway-openapi.json)

[Errors](errors.md): the one refusal shape every route uses.
