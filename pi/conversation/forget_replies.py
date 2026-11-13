"""The replies of Pi's delete-forever routes (`/forgetting/chats`), described in OpenAPI (D-01).

They name what pi.api.routes.forgetting already sends from pi.conversation.forget_queue: counts,
ids and states, never a title or a word of a chat. The routes describe them with
`pi.core.models.described` and do not re-validate the reply.
"""

from typing import Literal

from pi.core.models import Strict


class ChatForgetPlan(Strict):
    """What deleting one named chat reaches (forget_queue.summary) and the confirmation to send."""

    sessionId: str
    sessionIds: list[str]
    messages: int
    turns: int
    approvalRequests: int
    confirmation: str
    alreadyForgotten: bool


class ChatForgetResult(Strict):
    """How a queued chat ended at a start (forget_queue._record); `receiptId` only when done."""

    sessionId: str
    state: Literal["done", "refused", "retrying"]
    receiptId: str | None
    finishedAt: float
    detail: str | None


class ChatForgetOverview(Strict):
    supervised: bool
    pending: list[str]
    results: list[ChatForgetResult]
    chats: list[ChatForgetPlan]


class ChatForgetDone(Strict):
    """200: every named chat was already forgotten, so nothing was queued."""

    state: Literal["done"]
    sessions: list[str]


class ChatForgetQueued(Strict):
    """202: the chats are queued and Pi restarts to forget them before it opens again."""

    state: Literal["restarting"]
    sessions: list[str]
