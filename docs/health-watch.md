# Health watch

Conker tells the owner when one of its parts stops working, instead of the owner finding out
himself. One Inbox card per broken part; the card closes by itself when the part works again.

## What is checked

Every 15 minutes, while the owner's setting `server.health_watch` is on (the default; it lives in
the [owner settings](owner-settings.md) document):

| Check | Signal | Broken when |
| --- | --- | --- |
| `toolgate` | ToolGate's agent status with Pi's key | ToolGate does not answer or refuses the key (its lockdown counts as working) |
| `web_search` | `checks.searxng` on ToolGate's own `/health` page | the search service does not answer (no search query is sent) |
| `memorygate` | MemoryGate's `/health` | MemoryGate is unreachable or reports a problem |
| `memory_delivery` | Pi's memory outbox | a message is blocked, or still pending after 5 tries |
| `answer_model` | the answer role's model: its provider's `health()` | the provider is unreachable or not set up (no key, sign-in or download) |

No check calls a model, runs a search or spends anything. A check that cannot tell (the part is
not set up, or ToolGate is down so its search page cannot be read) changes nothing.

## Cards

- Two failures in a row file one card; one failed check is a blip and files nothing. A check
  that works in between starts the count again.
- One open card per check at most; while it stays broken, the card keeps the latest reason.
- The card is a **Needs you** item in [the Inbox](inbox.md) with the shape of a continuity
  attention item: `kind: "attention"`, `status: "failed"`, `urgency.basis: "health-check"`,
  `source: {"kind": "health-alert", "alertId", "check"}`, and as `title` one plain sentence, for
  example "Web search is not working: the search service does not answer." Reasons never carry
  an address, an id or an error text.
- When the check works again the card closes and shows under **Handled** for the rest of the
  owner's day: "Web search works again (fixed at 14:20)".
- Dismissing hides the card until its check works again; a later failure files a new one.
- With the setting off, no check runs and no card is shown.

## Routes

Recovery credential only (the owner-control browser path is not open to them yet):

- GET `/health-watch`: whether the watch is on, each check's latest outcome (`works`,
  `failures`, `reason`, `checkedAt`) and the open cards.
- POST `/health-watch/alerts/{id}/dismiss`: hide one card.

The current dashboard Inbox reads its own sources (approvals, change requests, asks, AI job
notes, ideas, password alerts), not Pi's `GET /inbox`, so the cards show in `conker inbox` and
`GET /inbox` until the dashboard reads them.
