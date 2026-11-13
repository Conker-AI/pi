"""The replies of Pi's chat context routes (`/context/{session_id}` and its summary), described
in OpenAPI (D-01).

They name what pi.context.controls and summaries already send; the routes describe them with
`pi.core.models.described` and do not re-validate the reply.
"""

from pi.context.controls import PolicyName
from pi.core.models import Strict


class ChatContextBudget(Strict):
    contextWindowTokens: int
    outputReserveTokens: int
    otherInputTokens: int


class ChatContextPolicy(Strict):
    """The saved policy, as pi.context.controls.Policy validated it."""

    sessionInstructions: str
    messagePolicies: dict[str, PolicyName]
    budget: ChatContextBudget


class ChatContextPolicyDocument(Strict):
    """GET and POST /context/{session_id}: revision 0 and no policy until the first save."""

    revision: int
    policy: ChatContextPolicy | None


class ChatSummaryVersion(Strict):
    """One kept summary; `restored_from` names the revision a restore copied."""

    revision: int
    summary: str | None
    source_session_id: str | None
    created_at: float
    restored_from: int | None


class ChatSummaryDocument(Strict):
    """GET and POST /context/{session_id}/summary: the current summary and the newest 100
    versions, newest first."""

    revision: int
    summary: str | None
    source_session_id: str | None
    versions: list[ChatSummaryVersion]
