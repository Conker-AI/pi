# Hide secrets from online models

The owner's `privacy.redact` (Settings > Privacy and data > Hide secrets from online models).
Off unless the owner turns it on, which is the registry's default. Code: `pi/models/redaction.py`
(where it applies), `pi/models/redaction_pem.py` (private key blocks) and
`pi/models/redaction_shapes.py` (keys, tokens, passwords, card numbers).

## Where it applies

Every request Pi sends to a model goes through the adapters built once at startup, so the one
wrapper put around each of them (`redaction.wrap` in `pi/api/startup.py`) sees every request
that can leave this server: chat turns, model roles, helper calls, service calls from MemoryGate
and ToolGate, proposals, teams, probes and evaluations. Online means OpenRouter, the direct-key
providers, the ChatGPT and Claude subscriptions, a decision service that is not on this server's
loopback, and Ollama anywhere but here.

Ollama counts as local only when its URL's host is a loopback address or `ollama`, its service
on this server's own network, and then only for its local models: a cloud model (a `-cloud`
name or a `cloud` tag, such as `gpt-oss:120b-cloud` or `glm-4.6:cloud`) is answered online and
is redacted. Ollama on another machine, or reached by any other name (`host.docker.internal`, a
LAN address), counts as online. A decision service on loopback gets every request as it is.

The setting is read on every request, so a change applies to the next model call.

## What it removes

Each secret becomes a fixed marker; nothing else in the message changes.

| Marker | What is found |
|---|---|
| `[removed: private key]` | A `BEGIN ... PRIVATE KEY` block with its header and key lines, and each half of one cut in two (a BEGIN line with the lines after it, or lines with the END line after them) |
| `[removed: key]` | `sk-...`, Stripe `sk_live_`/`sk_test_`/`rk_live_`, GitHub `ghp_` (and `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`), Slack `xox[abprs]-`, AWS `AKIA`/`ASIA`, Google `AIza`, Hugging Face `hf_`, npm `npm_`, JSON web tokens, the token after `Bearer`, with the random-looking lines right after one cut by a line wrap; and the value after a name ending in `_key`, `-key`, `_token` or `-token` (`GITHUB_TOKEN=`, `X-Api-Key:`) when it looks like a key: 8 characters with a digit, or 32 of anything |
| `[removed: password]` | Any value after a name ending in password, passwd, passphrase, pwd, secret, `_pass` or `-pass` and `:` or `=`, short or plain (`password=hunter`) |
| `[removed: card number]` | 13 to 19 digits (spaces or dashes allowed between groups) with a card network's prefix and length that pass the Luhn check |

What stays:

- A name that does not end at its separator (`password_length=12`, `max_tokens=4096`).
- Placeholders: empty, `null`, `true`, `****`, `xxx`, `changeme`, `<your password>`,
  `${DB_PASSWORD}`, `$DB_PASSWORD`, `{{ vault_pw }}`, `%DB_PW%`, a type (`password: str`).
- Code: a call or an index (`getpass()`, `os.environ["X"]`), and a reference to where a secret
  is kept (`settings.OPENAI_API_KEY`, `self.password`).
- A directory after pwd (`pwd: /home/user`, `PWD=C:\srv`).
- After a key's name, a value too plain to be a key (`primary_key: id`, `sort_key=created_at`).
- Dates, phone numbers, git SHAs, UUIDs, IBANs and ordinary numbers.

Prose after a password's name is taken too: in `secret: Santa is real`, "Santa" is removed.

Key lines are found however they are wrapped: real line breaks, JSON escapes at any depth (a
tool reading a JSON file, `terraform output -json`), HTML `<br>`, spaces, and a quoting or
comment mark at the start of each line (`> `, `# `, `// `, `| `).

Every message the model is given is cleaned: system prompts, memories, the owner's and Conker's
messages, tool results and attachment text. The stored chat, the history objects and the model's
answer, with any tool request it makes, are never changed. A replacement never takes a quotation
mark or half of a JSON escape, and one that stands for an unquoted JSON value
(`{"password": 12345678}`) is quoted the way the member's name is, so tool requests and results
stay valid JSON at any depth.

Every search reads the text in one linear pass: 2 MB of BEGIN or END lines, or of anything
else, takes well under a second.

## Fail closed

A setting Pi cannot read counts as on, and so does any stored value but `false` (`"true"`, `1`,
`"on"`); unset follows the registry's default, off. A request Pi could not clean is not sent:
the call fails as a model refusal (nothing billed), and the role moves on to its fallback.

## Receipt

How many of each kind were removed, never the values, is kept on the call's line in the turn's
usage receipt: `GET /turns/{turn_id}/usage`, `attempts[].redacted`, for example
`{"key": 2, "card": 1}`, or `null` when nothing was removed. Calls outside a turn have no receipt.

## Not covered yet

- A secret without a known shape: a password in a sentence with no name before it, the part of a
  token sent in another message, a `user:password@host` address, `Authorization: Basic`,
  `mysql -pPASSWORD`, Stripe `whsec_`.
- Key formats other than PEM (PuTTY `.ppk`), a key's lines without its BEGIN or END line, and a
  key split across quoted string literals in code.
- Card numbers written with non-ASCII digits.
- Images and audio. Speech (`PI_SPEECH_URL`) is sent as it is.
- ToolGate's own Gemini adapter, which is outside Pi.
