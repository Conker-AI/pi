# Model calls for Conker's services

MemoryGate and ToolGate need a model for some of their work: MemoryGate to sort what it hears into
memories, ToolGate to write the text a tool step asks for. `POST /inference` lets them use the
model the owner chose for that job in Pi's model roles, so every AI job in Conker picks its model
in one place (Settings, Models, Model roles). The call takes Pi's normal role path: the role's
model, its timeout and fallback, the providers' paid-model opt-in, the one-at-a-time local model
queue and cancellation. It is not a side path to a provider.

## Roles

| Role | Service | What it does |
|---|---|---|
| `memory-sorting` | `memorygate` | Sorts what MemoryGate hears into memory candidates and short summaries. |
| `tool-text` | `toolgate` | Writes the text a ToolGate tool step asks a model for. |

Until the owner picks a model for one of these roles (or while the role is switched off), it uses
the local model Pi runs (`PI_MODEL` on `PI_OLLAMA_URL`; on a standard install, the same Ollama and
model the services used before). Nothing leaves the server until the owner chooses a hosted model. That local default waits up to
300 s, because a cold local model on a small server can take minutes; a wait saved in the role's
settings is kept. Each service may use only its own role; this is fixed in code.

## Authentication

Each service has its own key and sends it as `X-Pi-Service-Key`. Pi reads the keys' SHA-256 hex
digests from `PI_SERVICE_KEYS_SHA256`, a JSON object:

```json
{"memorygate": "<sha256 hex>", "toolgate": "<sha256 hex>"}
```

Absent or empty, the route answers 401 to everyone. Pi refuses to start when the value is not such
an object, names another service, repeats a key, or reuses one of Pi's other credentials. Keys are
compared as hashes in constant time. The owner, gateway and recovery keys cannot call this route.

## Request and answer

```json
{"role": "memory-sorting", "system": "...", "prompt": "...", "max_tokens": 160,
 "json": true, "request_id": "memorygate:sort:0001"}
```

- `system`: at most 16,000 characters, may be empty. `prompt`: 1 to 64,000 characters.
- `max_tokens`: 8 to 4,096. The local model stops there; an answer from any model longer than
  16 characters per allowed token is refused as `unavailable`.
- `json`: asks for JSON output where the provider supports it (Ollama's `format: "json"`). Other
  providers get the prompt unchanged. Service calls ask the local model not to think, as the
  services did before.
- `request_id`: 16 to 128 of `A-Z a-z 0-9 _ . : -`. It is recorded with the call's usage so a
  service can match its logs; Pi does not replay answers, so a repeated id is a new call.
- Unknown fields are refused.

One request makes one role call: the role's model, then its fallback if the owner set one, and no
retries. The whole request never runs longer than the role's wait for each model it may try.
Memory sorting yields the local model to the owner's replies; ToolGate's text does not. A service
may have 4 calls running at once; the next one gets `unavailable`.

The answer is `200 {"text": str, "model": str, "provider": str}`: the model that answered and its
provider.

## Errors

| Status | When |
|---|---|
| 401 | The key is missing or wrong, or no keys are configured. |
| 403 | `role_not_allowed`: the role is not this service's. |
| 422 | The body breaks the rules above. |
| 503 | `{"detail": {"code": "no_model", "message"}}`: the role has no usable model (no local model, or the chosen model's provider is not set up). |
| 503 | `{"detail": {"code": "unavailable", "message"}}`: the model failed, timed out, was stopped, answered past `max_tokens`, or the service has too many calls running. |

On a 503 the service falls back as its own documentation says.

## Usage

Each model attempt is recorded in `service_model_calls`: service, role, `request_id`, the model and
provider, how it ended, time taken, tokens and the reported cost (local models report none). No
prompt and no answer text is kept. Records older than 30 days are dropped.

`GET /inference/usage?limit=50` (owner credential or recovery key) returns the most recent calls
(`limit` 1 to 200) and `lastDay`: calls, failures, tokens and cost per service and role over the
last 24 hours. A cost total is `null` unless every call in it reported one.

```sh
python -m pytest tests/test_service_inference.py tests/test_model_roles.py -q
```
