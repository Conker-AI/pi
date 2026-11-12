# Daily spending cap

One cap over every paid model call Pi makes, for the owner's local day (the time zone of his
quiet hours). Code: `pi/models/spending_cap.py`.

## What counts

- OpenRouter models that are not listed as free, when paid models are on
  (`PI_ALLOW_PAID_MODELS`). If the catalogue cannot be read, the call counts as paid.
- The direct-key providers (OpenAI, Anthropic, Gemini, xAI, Mistral, DeepSeek, Groq).
- Not counted: voice (`PI_SPEECH_KEY`), subscriptions (ChatGPT, Claude: flat-rate, shown as plan usage), local models
  (Ollama) and free OpenRouter models.

Every caller goes through the same adapters, so the cap covers chat turns, model roles,
service calls from MemoryGate and ToolGate, proposals, teams, probes and evaluations alike.
ToolGate's own Gemini adapter is outside Pi and keeps ToolGate's spending budget.

## The cap

`spendingCap` in owner preferences (`POST /owner/preferences`, which asks for the password in
the browser):

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Off: calls are still counted, never stopped. |
| `cents` | `500` | Money for the day, from the cost the provider reports. |
| `tokens` | `2000000` | Input plus output tokens of every paid call. |

Direct keys report tokens but no price, and Pi never guesses a price, so the token limit is the
one that stops them. A call that reports no tokens is counted by its length (4 characters a
token) and marked estimated.

## The stop

Before a paid call is sent, Pi checks the day's totals. When either limit is reached the call is
refused with a model refusal (`CapReached`, a `PaidModelRefused`): a role moves on to its
fallback, so a free or local fallback keeps answering, and nothing paid is sent until the next
local day. If Pi cannot read its ledger, paid calls are refused the same way (`CapUnavailable`).

Each call holds its input's tokens (estimated from its length) from the moment it is admitted,
so calls in flight together cannot all pass under the cap; its output and price are added when
it ends, so a day can end over the cap by at most the answers that were in flight. A call that
fails after it was sent keeps its input hold, since it may have been billed; a model refusal
(the provider said the model will not serve) counts nothing. A call running when Pi stopped is
marked failed at the next start and keeps its hold.

The money limit binds only where the provider reports a price (OpenRouter). For direct keys the
token limit is the stop: 2M tokens of an expensive model can cost more than $5.

## The meter

`GET /inference/spending` (owner credential; browser read):

```json
{"day": "2026-11-12", "timeZone": "Asia/Jerusalem",
 "cap": {"enabled": true, "cents": 500, "tokens": 2000000},
 "used": {"costUsd": 0.025, "tokens": 300, "calls": 2, "running": 0, "unpricedCalls": 0,
          "estimatedTokenCalls": 0, "failedCalls": 0},
 "reached": []}
```

`reached` lists `money` and/or `tokens` once a limit stops paid calls. The ledger
(`paid_model_calls`) holds provider and model ids, counts and states, never a prompt or an
answer, and keeps 40 days.
