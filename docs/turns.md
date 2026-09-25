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

## Status vocabulary

`/health` reports `ok`, `degraded`, `unavailable`, `not_configured` or `unknown` per check.
`not_configured` is **not** a failure. Nothing is ever `ok` because it was configured — every check
is probed.
