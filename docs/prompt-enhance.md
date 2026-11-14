# Prompt enhance

The wand in the composer: Jev rewrites the owner's unsent draft into a clearer, better organised
prompt (goal or role, context, task, wanted output format). The owner presses it; nothing runs by
itself, nothing is sent and nothing is saved. The dashboard puts the result in the text box with
Undo and Show original.

Route: `POST /prompts/enhance`, owner session or admin key, no password.
CLI: `conker prompt enhance "text"`.

Body (`Request`):

| Field | Meaning |
|---|---|
| `text` | the draft, 1 to 8000 characters, not blank |
| `sessionId` | optional; the chat the draft is for, so its privacy applies |
| `privacy` | optional `memoryDisabled` / `harnessDisabled` of a chat that is not saved yet; it can only add privacy, never remove it |
| `templateId` | refused with 422 `templates_unavailable`: prompt templates live only in the dashboard, so this version only rewrites |

Reply (`EnhancedPrompt`): `text` (the rewrite), `notes` (up to 5 short notes on what changed),
`modelId` (the catalogue model that wrote it), `templateId` (always `null` for now) and `fields`
(always empty for now).

What is sent: one model call. The model gets fixed instructions and the draft, nothing else: no
memory, no history, no agent text. It uses the drafting model when the owner set one, otherwise
the answer model (with the answer role in router mode the routing model first reads the draft).
A chat with helper models off is refused (409 `helpers_off`) because the draft would reach a
helper model; a chat with only memory off is allowed, since no memory is read. The call counts
in the daily spending cap like every paid call (`docs/spending-cap.md`): when the cap is reached,
or Pi cannot check it, the call is not sent and the answer says so (503 `spending_cap_reached`,
503 `spending_cap_unavailable`). A free or local model is not counted.

The reply is untrusted text the owner reads before sending it: hidden and control characters are
removed, a reply over 16000 characters or cut at the model's length limit is refused (502
`unusable`), and a model that returns the prompt as plain text or in a fenced block is accepted.
There is no second try, so one press is at most one model call.

Other refusals: unknown chat 404 `not_found`, no model 503 `not_configured`, the model did not
answer 503 `unavailable`. Messages never contain the draft or the provider's own text.
