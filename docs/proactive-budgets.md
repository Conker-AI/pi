# Proactive admission reservations

Owner-authenticated `GET /continuity/budget` reports reserved suggestions,
research minutes and cost cents for the current local day and preference revision.
`POST /continuity/budget/reserve` takes request_id, expected_revision, proposed
usage and explicit urgency. Both responses are no-store.

Admission runs under a SQLite writer lock against current owner preferences.
Quiet hours, urgency, disabled proactivity and daily ceilings apply before a
reservation is inserted. Repeated IDs must carry exactly the same revision,
amounts and urgency; a lost reply returns the original receipt. No automatic
refund, reset or increase exists. Changing the time zone reclassifies existing
timestamps into the current local day rather than starting a fresh budget.

Reservations contain no prompt, transcript or source title. These are planned
upper bounds, not measured usage or execution authorization. A recovered receipt
after preferences change is historical evidence, not permission to repeat work.
The worker must retain a durable execution identity, reserve before starting,
and enforce its duration/usage limits. Paid effects additionally need ToolGate's
bounded spending authority. The polling notification feed does not consume these
reservations because it only projects existing status changes.

This API establishes the missing atomic admission ledger. Autonomous suggestion
and research dispatch are not activated by creating a reservation. Their binding
to execution remains an acceptance item; no background task or paid service was
started by this change.
