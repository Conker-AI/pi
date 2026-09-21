# Companion continuity feed

Owner-only `GET /continuity` returns unseen, recorded task/run status changes with
source session/task/run IDs, timestamps and attention flags. It does not generate
text, poll providers, infer completion or mark items read. `POST /continuity/seen`
accepts explicit event IDs and persists acknowledgement atomically. Refreshing or
reopening a page cannot consume unseen work; acknowledging an older status does
not acknowledge a later change.

Each entity contributes only its latest status. Completed work and explicit
failure/blocker/approval/budget/uncertain/interrupted states qualify; startup,
ordinary editing and running-state noise do not. The event sequence is the page
cursor. Each request scans at most 1000 candidate rows and returns at most 100
items; its cursor permits continued scanning when privacy filtering yields an
empty page. It resolves current titles and excludes private/forgotten sessions
rather than storing another copy of conversation content.

The result includes the current preference revision and notification suppression
reasons. Quiet hours and disabled proactivity suppress notification eligibility;
`urgent_only` currently suppresses all candidates because this feed does not
invent an urgency classification. Owner-initiated inspection remains available.

This is the durable input to the Companion's while-away presentation, not an
outbound notification service. `notificationDelivery=not-configured` and
`summaryGeneration=none` are explicit. Research/spending reservations, automatic
briefing generation, scheduled-job outcomes outside the task/run event ledger,
and push/call notification transports remain separate acceptance work. No daily
budget is claimed to be enforced merely by rendering these preference flags.

Tests cover actual event triggers, repeated reads, restart-safe acknowledgements,
later status changes, paging, atomic rejection of invalid IDs, private/forgotten
sources and quiet-hour behavior. Frontend transport remains unwired.
