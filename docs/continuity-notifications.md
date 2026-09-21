# Owner notification delivery

The continuity briefing now includes a deterministic summary of its returned page:
completed/attention update counts and up to five source-linked highlights, with
attention first. `scope=returned-page` and `hasMore` prevent a partial page from
claiming whole-history totals. Counts are status updates, not unique completed
tasks. Read/privacy/forgetting filters apply before summary construction. This
uses no model and adds no generated factual claims or research costs.

GET `/continuity/notifications` is an owner-authenticated, no-store polling channel
for meaningful task/run/job status changes. It reuses continuity's latest-status,
privacy and quiet-hours selection. Responses have stable `eventId` keys; clients
must deduplicate those IDs and paginate with `nextCursor`, including empty pages.

After displaying an item, POST its ID to `/continuity/notifications/delivered`.
Acknowledgement is durable, atomic and idempotent across reconnects/restarts. It
does not mark the event read; `/continuity/seen` remains the separate read action.
A lost delivery acknowledgement may cause redelivery with the same ID. This is
at-least-once delivery, not a claim of exactly-once network delivery.

Quiet hours and disabled urgency modes suppress delivery without
consuming events. Completed work replaces older blockers through existing status
selection. No title/content copies are stored in delivery records, so privacy and
forgetting continue to apply at projection time. No external notification provider,
background browser push, model-generated prose, or final UI wiring is configured.
This channel makes no paid model calls and does not consume suggestion budgets.

Urgency is grounded in status: `outcome_unknown` is urgent because an external
effect needs reconciliation before a safe retry. Ordinary failures, approvals and
completed work are meaningful but not automatically urgent. `urgent_only` filters
each event; `urgentExceptions` bypasses quiet hours only for urgent events. `off`
still suppresses all delivery. Briefings retain all inspectable events and report
per-event suppression separately. No LLM or message content determines urgency.
Cancelled updates are counted separately from completed/attention updates.
