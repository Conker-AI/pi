# Self-check

Pi checks itself and tells the owner in the Inbox when a part of it stops working. Code:
`pi/owner/self_check.py`.

Every 5 minutes (`PI_SELF_CHECK_SECONDS`) a background worker reads the same checks as
`GET /health`: Pi's database, memory, the local and hosted models, provider keys, voice,
ToolGate and the clean-up after deleted chats. The first check runs one interval after start,
so services that start together are not reported as broken.

- A check that stays `unavailable` or `degraded` for 15 minutes raises one alert: an Inbox item
  of kind `health` ("Memory is not working") with its status, its reason and since when. A blip
  shorter than that raises nothing.
- When the check works again the alert closes; the Inbox's Handled list shows
  "Memory works again" (kind `health-recovered`) for that day.
- `not_configured` and `unknown` (configured, not proven) are never alerts.
- The owner may dismiss an alert (`POST /self-check/alerts/{id}/dismiss`, the signed-in session
  is enough). It stays hidden until the check recovers and breaks again.

`GET /self-check` (owner credential; browser read) lists each check's last status and since
when, and the open alerts. The rows hold check names, statuses and the checks' fixed reason
strings, never a chat or a key.

Limits: it sees only what Pi sees. If Pi itself is down nothing here runs, and an alert reaches
the owner only when the dashboard is open (no push yet).
