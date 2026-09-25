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
