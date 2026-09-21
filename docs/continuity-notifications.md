# Owner notification delivery

GET `/continuity/notifications` is an owner-authenticated, no-store polling channel
for meaningful task/run/job status changes. It reuses continuity's latest-status,
privacy and quiet-hours selection. Responses have stable `eventId` keys; clients
must deduplicate those IDs and paginate with `nextCursor`, including empty pages.

After displaying an item, POST its ID to `/continuity/notifications/delivered`.
Acknowledgement is durable, atomic and idempotent across reconnects/restarts. It
does not mark the event read; `/continuity/seen` remains the separate read action.
A lost delivery acknowledgement may cause redelivery with the same ID. This is
at-least-once delivery, not a claim of exactly-once network delivery.

Quiet hours and disabled/unsupported urgency modes suppress delivery without
consuming events. Completed work replaces older blockers through existing status
selection. No title/content copies are stored in delivery records, so privacy and
forgetting continue to apply at projection time. No external notification provider,
background browser push, model-generated summary, or final UI wiring is configured.
This channel makes no paid model calls and does not consume suggestion budgets.
