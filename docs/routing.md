# Model routing and roles

Which model answers a turn, why, and the independent helper roles.

## Routing

Routing is not a feature - it is the cost structure of a system that runs all day. A **local model
carries ordinary conversation**, and Pi escalates only on explicit signals:

| Signal | Reason recorded |
|---|---|
| The owner asked for a stronger model | `owner_asked` |
| The turn needs tools | `tools_required` |
| A previous attempt failed | `retry_after_failure` |
| The work is analysis, not conversation | `analysis` |
| History has grown large | `long_context` |

Every route is recorded on the turn with its **reason**, because a policy nobody measures drifts
into always escalating - and it is a known trap: a cheap model that fails and
then escalates has cost both.

The router is deliberately **not** a classifier reading the message. That would be a model nobody
evaluates deciding what every turn costs. It sees only facts the loop already has, and the caller
passes what it genuinely knows.

**Free by default.** Models are **discovered, not hardcoded** - a static list is stale within weeks.
Paid models are refused unless `PI_ALLOW_PAID_MODELS` is set, and a model outside the catalogue is
refused rather than called blind, so a typo cannot become a bill. An unreadable price counts as
**not free**, because treating it as zero is exactly the assumption that produces one.

Only text-in, text-out models are routed to. Some zero-priced entries are audio or image generators
- Google's Lyria outputs `["text", "audio"]` - and routing a conversation into one is a strange
failure to diagnose from the answer alone.

**Listed and free does not mean callable.** Some free models are gated to particular clients and
answer `403`. Pi walks its candidate list rather than failing the turn, and **records what it
skipped** - so a model that always refuses is visible in the record rather than only as latency. A
bad key or an exhausted quota is *not* treated this way: those fail identically on every candidate,
and walking the catalogue would be slow and would blame the models.

`GET /models` shows what Pi can route to right now, free and paid, with the count discovered.

## Independent model roles

The catalogue assigns answer generation, model routing, context selection, summarization and memory ranking independently. Memory ranking replaces the legacy PI_MEMORY_RERANK_ENABLED switch and migrates disabled. Enable it explicitly in Conker Settings. The optional decision-service adapter accepts `model-routing` and `memory-ranking` routes; general completion adapters can also implement the ranking JSON contract. Provider transports and credentials remain server-owned.

Memory ranking only reorders authorized candidates; it preserves originals and source links. Invalid or unavailable results keep retrieval order. No-memory skips retrieval; no-harness skips ranking. See [decision-service setup](https://github.com/Conker-AI/conker/blob/main/services/decisions/README.md).

## Local model settings

Three of the owner's settings (`pi.owner.settings`, read by `pi/models/local_settings.py`) shape
every request Pi sends Ollama: chats, helper roles and the services' `/inference` calls alike.
Hosted providers never read them. Each is read on every request, so a change applies to the next
one. A value of the wrong type counts as unset; an unset one sends nothing, so Ollama's own setting
applies.

| Setting | Sent as | Range |
|---|---|---|
| `models.local.keep_loaded` | `keep_alive` | Minutes after the last use, kept to 5..1440 (a number or a string of digits); `"until_restart"` sends `-1`. Unset: the server's `OLLAMA_KEEP_ALIVE` (24h on a standard install). |
| `models.local.context` | `options.num_ctx` | Tokens, kept to 2048..32768 and never above the server's context length. Unset: the server's `OLLAMA_CONTEXT_LENGTH`. |
| `models.local.threads` | `options.num_thread` | 1 to the host's processor threads; 0 or unset leaves Ollama's own choice. |

Pi knows the server's context length from `PI_OLLAMA_CONTEXT_LENGTH`, or `OLLAMA_CONTEXT_LENGTH`
when it shares Ollama's environment; the installer sizes that to the machine, so the owner's
setting can only lower it. Ollama's API does not report the server's length (and Ollama already
keeps a request to the model's own trained length). When Pi does not know it, a context too large
for the machine's memory makes local answers fail: Ollama refuses to load the model, or its
container runs out of memory and restarts. Pi reports that as an unavailable model, as it does any
Ollama failure, and never crashes; a smaller setting recovers. A changed context or thread count
makes Ollama load the model again once, so the next local answer waits for the load.
