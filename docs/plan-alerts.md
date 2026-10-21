# Plan usage alerts

A heads-up in the Inbox when a ChatGPT or Claude subscription plan passes the owner's level, and
another when that plan window resets. Usage is a percentage of each window (the 5-hour and weekly
limits), as the subscription reports it. Pi never estimates prices or tokens.

## How it works

- Each subscription runtime answers `GET /limits` on Pi's inference channel with the plan's
  windows: `usedPercent` and `resetsAt`. The runtime reads them the same way `conker limits` does,
  without a model call.
- While `planAlerts.enabled` is on in the [owner preferences](owner-preferences.md), Pi reads every
  configured runtime every 15 minutes (`PI_PLAN_CHECK_SECONDS`). The worker runs only when a
  subscription runtime is configured.
- A window at or above `planAlerts.percent` (50 to 100, default 80) raises one alert. Further
  readings in the same period raise the recorded peak, not new alerts.
- When the window's reset time passes, or the runtime reports a later reset time, the alert closes.
  A reset time that moves by less than a minute counts as the same period.
- A new period can raise a new alert.

## Where it shows

- [Inbox](inbox.md): an open alert is a **Needs you** item, `kind: "plan"`, for example "ChatGPT
  weekly limit 84% used" with `resetsAt`. A reset since the start of the owner's day is a
  **Handled** item, `kind: "plan-reset"`.
- GET `/plans/usage` (admin) returns the latest reading of every window and the open alerts.
- POST `/plans/alerts/{id}/dismiss` hides an alert until its window resets. Dismissing does not
  stop a later period from alerting.

Readings that do not parse, or a runtime that is unreachable, are logged without content and
retried at the next check. Nothing is sent outside the machine, and no notification is pushed.
