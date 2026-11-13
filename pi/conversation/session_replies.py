"""The reply of GET /sessions/{session_id} (one chat with its messages and turns), described in
OpenAPI (D-01).

It names what pi.api.routes.sessions already sends from the sessions, messages and turns rows,
pi.conversation.message_view, pi.memory.store.statuses and pi.turns.thinking; the route describes
it with `pi.core.models.described` and does not re-validate the reply. Message content, a parked
action's arguments and a recalled memory package are open JSON, as stored.
"""

from typing import Literal

from pydantic import JsonValue

from pi.conversation.citations import Citation
from pi.core.models import Strict


class ChatMemoryAdmission(Strict):
    """How many saved messages MemoryGate admitted, filtered out or deleted."""

    admitted: int
    filtered: int
    deleted: int


class ChatMemoryRetrieval(Strict):
    """What memory a turn was given; `turn_id` and `recorded_at` only when it was recorded."""

    status: str
    package: JsonValue
    turn_id: str | None = None
    recorded_at: float | None = None


class ChatMemoryStatus(Strict):
    """Memory delivery of the chat's messages; `retrieval` is null for the chat, set per turn."""

    configured: bool
    pending_ingestion: int
    blocked_delivery: int
    pending_deletion: int
    admission: ChatMemoryAdmission
    delivery_error: str | None
    retrieval: ChatMemoryRetrieval | None
    notices: list[str]


class ChatMessageMemoryDelivery(Strict):
    """What became of one owner message in long-term memory (pi.memory.delivery)."""

    state: Literal["saved", "filtered", "pending", "excluded", "removed", "unavailable"]
    reason_code: (
        Literal[
            "forgotten",
            "legacy_unknown",
            "privacy_disabled",
            "agent_excluded",
            "forget_pending",
            "retry_scheduled",
            "queued",
            "blocked",
            "invalid_receipt",
            "destination_unknown",
        ]
        | None
    )
    admission_method: Literal["owner_requested", "signal", "filtered"] | None
    memory_id: str | None


class ChatResponseFamily(Strict):
    """The answer versions a message belongs to, and which one is selected."""

    root_message_id: str
    retry_of: str | None
    selected_message_id: str
    revision: int


class ChatAttachmentPrivacy(Strict):
    memoryDisabled: bool
    harnessDisabled: bool
    incognito: bool


class ChatAttachmentProcessing(Strict):
    status: str
    reason: str


class ChatMessageAttachment(Strict):
    """A file sent with a message; an unreadable one keeps its id and size but not its name."""

    id: str
    sessionId: str
    name: str
    size: int
    type: str
    lastModified: float
    createdAt: float
    sha256: str | None
    availability: Literal[
        "available", "source-archived", "removed", "source_unavailable", "privacy_unknown"
    ]
    privacy: ChatAttachmentPrivacy | None
    privateOrigin: bool | None
    processing: ChatAttachmentProcessing
    execution: Literal["not-wired"]
    storage: str


class ChatMessage(Strict):
    """One message. A forgotten one carries `content_status`, `receipt_id` and `forgotten_at`;
    the other optional keys are present only when they apply."""

    id: str
    session_id: str
    seq: int
    role: str
    content: JsonValue
    created_at: float
    receipt_id: str | None = None
    forgotten_at: float | None = None
    content_status: Literal["forgotten"] | None = None
    citations: list[Citation] | None = None
    memory_delivery: ChatMessageMemoryDelivery | None = None
    response_family: ChatResponseFamily | None = None
    turn_id: str | None = None
    agent_id: str | None = None
    reply_to: str | None = None
    attachments: list[ChatMessageAttachment] | None = None


class ChatTurnAction(Strict):
    """The turn's latest tool action."""

    id: str
    state: str
    job_id: str | None


class ChatTurn(Strict):
    """One turn row; `acted` is 0 or 1."""

    id: str
    session_id: str
    status: str
    provider: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    cost_usd: float | None
    latency_ms: int | None
    route_tier: str | None
    route_reason: str | None
    approval_request_id: str | None
    approval_tool_id: str | None
    approval_args: JsonValue
    approval_expires_at: str | None
    approval_intent: str | None
    acted: int
    started_at: float
    ended_at: float | None
    detail: str | None
    usage_version: int | None
    action: ChatTurnAction | None
    memory: ChatMemoryStatus


class ChatPendingSubmission(Strict):
    """A saved input not yet resolved; `status` is its turn's status, else its state."""

    request_id: str
    requested_session_id: str
    effective_session_id: str | None
    turn_id: str | None
    state: Literal[
        "preparing", "bound", "preparation_failed", "preparation_interrupted", "forgotten"
    ]
    created_at: float
    updated_at: float
    status: str
    content_status: Literal["available"]


class ChatBackgroundThinking(Strict):
    """Background work for one turn (pi.turns.thinking)."""

    turn_id: str
    session_id: str
    status: str
    created_at: float
    ended_at: float | None
    model: str | None
    provider: str | None
    result: str | None
    detail: str | None
    input_tokens: int | None
    output_tokens: int | None
    input_message_id: str | None


class ChatSessionDocument(Strict):
    """GET /sessions/{session_id}: the sessions row with what belongs to it."""

    id: str
    parent_id: str | None
    title: str
    status: str
    created_at: float
    closed_at: float | None
    summary: str | None
    pending_submissions: list[ChatPendingSubmission]
    pending_submissions_truncated: bool
    messages: list[ChatMessage]
    turns: list[ChatTurn]
    memory: ChatMemoryStatus
    thinking: list[ChatBackgroundThinking]
