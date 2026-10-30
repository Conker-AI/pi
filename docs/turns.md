# Turns: acting, recording and status

What happens to a turn that needs a tool, needs approval, acts but fails to reply, or is cut off by a restart, and the words Pi uses for each.

## Acting, and asking first

Pi **executes nothing itself**. Every action goes through ToolGate, which is the only thing that
can run one and the only thing that can approve one. Pi is given a *scoped* execution key and asks
ToolGate what that key may reach on every turn, rather than caching it — the owner can widen or
narrow scope at any moment.

When a tool needs confirmation the turn **parks**: status `awaiting_approval`, with the exact tool
and arguments stored whole. That is not a failure and is deliberately not recorded as one — the
owner has not said no, they have not been asked yet. A restart does not withdraw the question.

Resuming replays **the stored action**, not one rebuilt from the conversation, so an approval can
never be spent on a different action than the one the owner was shown. ToolGate consumes the nonce
once; a replay fails closed.

### An action that happened is never recorded as one that did not

A tool can succeed and the model can *then* fail to say so. The action is real, the approval is
spent, and the world has changed — so that turn is recorded as **`acted_no_reply`**, never `failed`,
and `POST /turns/{id}/resume` asks only for the missing reply without running the action again.

For the same reason neither `/turns` nor `/resume` answers with an error status in that case: an
error code invites a retry, and retrying the whole turn would do the thing twice. They answer `200`
with `status: acted_no_reply` and say plainly what is missing. `GET /turns/unreplied` lists them,
because an action whose result the owner never sees is, to them, the same as one that silently went
wrong.

## Turns are recorded, not just run

Every turn stores provider, model, input and output tokens, cached tokens, cost and latency. A
provider that does not report a price yields `null`, which renders as **unknown** — never `0`, which
would read as free.

**A turn that was running when the process stopped is marked `interrupted` at the next startup**,
with the reason. Saying nothing would leave the owner looking at a request that simply vanished.
A turn that had already **acted** before the process died says so — it is not the same event as one
that died before touching anything, and one message for both would describe the wrong one.

### What a restart does at each point of a turn

Pi never runs a model call or an action again on its own after a restart. The receipt by request id
(`GET /turn-submissions/{id}`) shows the outcome, and sending the same request id again only reads it.

| Pi stopped | After the restart |
| --- | --- |
| After the request was admitted, before its turn began | `preparation_interrupted`; the text is kept for the composer and no model was called. |
| During a model call | `interrupted`: "Pi restarted before the answer was finished. Your message is kept; send it again to retry." |
| After the answer was produced, before it was saved | The turn is completed with that answer. Pi holds each answer (`held_answers`) before the final commit; a stop or steer the owner sent still refuses it, as it would live. |
| After the answer was saved, before the browser heard | `complete`; the receipt points at the saved answer. |
| While a ToolGate action was in flight | `outcome_unknown`; resuming asks ToolGate about the same action id and never dispatches it again. |
| While one of Conker's own tools (`conker.*`) was in flight | Settled from Pi's records: if it filed its Inbox request or question, `acted_no_reply` (ask for the reply); if not, `interrupted` with a result saying nothing was done. |
| After an action's result was recorded, before the reply | `acted_no_reply`; resuming asks only for the reply. |

The final save can also fail while Pi keeps running (the database is locked, full or failing). Pi
tries it again three times, 0.25, 1 and 3 seconds apart. If it still fails the request answers "Pi
could not save the answer yet. It is kept and appears in this chat once the database works again."
and the turn stays `running` while Pi tries again every 15 seconds; the answer is saved as soon as
the database works, with no restart and no new model call. A stop or a steer sent meanwhile ends
the turn as it would at a restart. A save the database itself refuses (a constraint), or one still
failing after 40 tries while other writes work, ends the turn `failed` ("Pi could not save this
answer: its database kept failing. Your message is kept; send it again to retry.") or, if it had
acted, `acted_no_reply`, so the chat is never left waiting.

## Status vocabulary

`/health` reports `ok`, `degraded`, `unavailable`, `not_configured` or `unknown` per check.
`not_configured` is **not** a failure. Nothing is ever `ok` because it was configured — every check
is probed.
