# Web push: Conker reaches the owner's devices when the tab is closed

Off by default. The owner turns it on per device in Settings, Notifications ("Notifications on this
device"); nothing is sent while no device is on.

## Who does what

| Part | Does | Code |
| --- | --- | --- |
| Pi | Decides which Inbox items may wake a device and records each claim once. Sends nothing (ADR-0005: no outbound HTTP). | `pi/agents/push_queue.py`, `POST /push/claim` |
| Gateway | Keeps the server key and the device list, claims from Pi every 30 s while a device is on, encrypts and sends. | `gateway/push.py`, `gateway/push_routes.py` |
| Dashboard | The Settings switch subscribes this browser; the service worker shows the notification and opens the Inbox. | `dashboard/public/sw.js`, `dashboard/src/pwa/register.ts` (conker repo) |

## What may be pushed

Only items the Inbox lists under "Needs you" now (approvals, suggestions, things that need a
look, plan and health alerts, updates, Conker's requests and questions, job notes, memory notices),
so a forgotten chat's item never qualifies. Then the owner's own rules, with the same function
proactive work uses (`owner_preferences.admission_reasons`):

- proactivity off sends nothing; "urgent only" sends only urgent items (an action whose result is
  unknown);
- quiet hours hold everything except urgent items, and those only when the owner allowed urgent
  exceptions;
- at most the owner's daily suggestion number of pushes a local day (4 by default), counted in
  `push_deliveries`; pushes never use up the reservations the owner's jobs make.

A held item is not used up; it may go later while it still waits. Items that started waiting
before the first device was turned on are not replayed. Each item is claimed once, ever: the claim
is recorded in the same write transaction that checks it, before the gateway sends, so a retry, a
second claim at the same time or a restart never sends it twice. A claim the gateway could not send
is lost, not repeated; the Inbox still shows the item. `push_deliveries` holds a digest of the
item's kind and id and a time, never text.

## What a notification says

A fixed short title chosen by the item's kind ("Conker needs your approval", "Conker has a
question for you", ...) and the Inbox link. Never a chat title, a message, a name or an amount: the
lock screen is public. The payload is encrypted to the device (RFC 8291, aes128gcm, checked against
the RFC's example) and signed with the server key (VAPID, RFC 8292; the contact claim names the
software, not the owner's server).

## Keys and devices

- The server key is made on first use as `push-vapid.pem` in the gateway's data folder, next to the
  TLS key and the password verifier, 0600. Only its public half leaves the file, to the signed-in
  owner (`GET /api/push`). It is never logged or put in an error. An unreadable key turns push off
  (503) and nothing is claimed.
- A device is a browser's push subscription, kept in the gateway's database (`push_subscriptions`),
  at most 10. Only the push services of Chrome, Edge, Firefox and Safari are accepted as endpoints
  (`fcm.googleapis.com`, `android.googleapis.com`, `*.push.services.mozilla.com`,
  `*.push.apple.com`, `*.notify.windows.com`; HTTPS on 443), so the gateway never posts to an
  address inside the server's network. A device the push service reports gone (404, 410) is
  removed. Signing out everywhere and a new password turn every device off.

## Routes

| Route | Needs | Does |
| --- | --- | --- |
| `GET /api/push` | session | `{publicKey, devices: [id], limit}`; makes the key on first use |
| `POST /api/push/subscriptions` `{endpoint, keys: {p256dh, auth}}` | session, CSRF | turns this device on; `{device, devices, limit}` |
| `POST /api/push/subscriptions/{id}/delete` `{}` | session, CSRF | turns one device off; `{removed, devices, limit}` |
| Pi `POST /push/claim` `{after, limit?}` | owner-control key; the gateway's own call | `{items: [{kind, urgent, tag}], held, delivery}` |

A device id is the first 32 hex digits of the SHA-256 of its endpoint, so the page can tell
whether this browser is on. No password: a device that gets content-free notifications can be
turned off again (ADR-0010). The browser relay refuses `/push/claim`
(`browser_contract.GATEWAY_ONLY`), so no page can silence an item by claiming it.

## Rollback

New tables only (`push_deliveries` in Pi, `push_subscriptions` in the gateway) and one key file; an
older version ignores them and sends nothing. Updating again finds the devices still on.
