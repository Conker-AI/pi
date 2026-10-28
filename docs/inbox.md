# Inbox

Inbox is a read-only view of everything that happened and everything that needs the owner. It assembles information only from what Pi recorded, generates no text, calls no model, sends nothing, acknowledges nothing, and invents no numbers.

## Four Parts

**Needs you** contains items that require owner decisions: awaiting tool approvals, open companion suggestions, pending memory proposals, and continuity items needing attention (blocked, failed, approval, budget, uncertain, interrupted), [plan usage alerts](plan-alerts.md), [workflow drafts made from a chat message](workflow-drafts.md), and Pi's offers to draft one when the owner asks in chat for something recurring. Conker's [questions to the owner](inbox-answers.md) appear as kind `ask`. Urgent items appear first, then oldest first.

**Handled** shows completed work since the start of the owner's local day: completed tasks, runs, and jobs, and plan windows that reset. Items are listed newest first, limited to 50.

**Upcoming** displays scheduled jobs due within the next 7 days (soonest first) followed by open tasks (planned or in progress). Limited to 20 items total.

**Impact** provides exact counts from recorded data: answers (completed turns), actions (completed tool actions), tasks completed, jobs completed, and cost in USD. Separate totals for today and the last 7 local days.

## What Is Never Included

- Approval arguments and intent (may contain private content)
- Memory proposal text (may be private)  
- Private chat titles (shown as "Private chat")
- Any content from forgotten sessions
- Invented numbers or summaries

## Day Boundary

The owner's day starts at local midnight in the owner's configured time zone (from `quietHours.timeZone` in preferences). If no time zone is set, UTC is used.

## Limits

- Needs you: at most 50 items
- Handled: at most 50 items  
- Upcoming: at most 20 items
- All lists are truncated when limits are exceeded

## Read-Only

Reading the inbox never acknowledges continuity items or changes any state. It is a pure aggregation of existing records.