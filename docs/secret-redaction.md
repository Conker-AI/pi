# Hide secrets from online models

The owner's `privacy.redact` (Settings > Privacy and data > Hide secrets from online models).
Off unless the owner turns it on, which is the registry's default. Code: `pi/models/redaction.py`.

## Where it applies

Every request Pi sends to a model goes through the adapters built once at startup, so the one
wrapper put around each online adapter (`redaction.wrap` in `pi/api/startup.py`) sees every
request that leaves for an online model: chat turns, model roles, helper calls, service calls
from MemoryGate and ToolGate, proposals, teams, probes and evaluations. Online means OpenRouter,
the direct-key providers, the ChatGPT and Claude subscriptions and a decision service that is
not on this server's loopback. Ollama and a decision service on loopback get every request as it
is. The setting is read on every request, so a change applies to the next model call.

## What it removes

Each secret becomes a fixed marker; nothing else in the message changes.

| Marker | What is found |
|---|---|
| `[removed: private key]` | A `BEGIN ... PRIVATE KEY` block with its key lines, and each half of one cut in two (a header with the lines after it, or lines with the end line after them) |
| `[removed: key]` | `sk-...`, Stripe `sk_live_`/`sk_test_`, GitHub `ghp_` (and `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`), Slack `xox[abprs]-`, AWS `AKIA`/`ASIA`, Google `AIza`, JSON web tokens, the token after `Bearer` |
| `[removed: password]` | The value after a secret's name and `:` or `=`: password, passwd, passphrase, pwd, secret, an api, access, secret or private key, a client secret, an access, refresh, auth, api, bearer, session or id token |
| `[removed: card number]` | 13 to 19 digits (spaces or dashes allowed between groups) with a card network's prefix and length that pass the Luhn check |

Precision comes first: a token needs a digit, a name must end right at its separator
(`password_length=12` stays), and placeholders, references and code (`null`, `${DB_PASSWORD}`,
`getpass()`, `password=pw`) stay. Dates, phone numbers, git SHAs, UUIDs, IBANs and ordinary
numbers stay.

Every message the model is given is cleaned: system prompts, memories, the owner's and Conker's
messages, tool results and attachment text. The stored chat, the history objects and the model's
answer, with any tool request it makes, are never changed. A replacement never takes a quotation
mark or half of a JSON escape, so tool requests and results stay valid JSON.

## Fail closed

A setting Pi cannot read counts as on. A request Pi could not clean is not sent: the call fails
as a model refusal (nothing billed), and the role moves on to its fallback.

## Receipt

How many of each kind were removed, never the values, is kept on the call's line in the turn's
usage receipt: `GET /turns/{turn_id}/usage`, `attempts[].redacted`, for example
`{"key": 2, "card": 1}`, or `null` when nothing was removed. Calls outside a turn have no receipt.

## Not covered yet

- A secret without a known shape: a password in a sentence, the part of a token that was cut off
  into another message, a `user:password@host` address.
- Images and audio. Speech (`PI_SPEECH_URL`) is sent as it is.
- ToolGate's own Gemini adapter, which is outside Pi.
