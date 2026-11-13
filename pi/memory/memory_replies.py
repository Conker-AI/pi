"""The replies of Pi's owner memory routes, described in OpenAPI (D-01).

They name what the routes already send: the owner library page (`/memory/objects`, MemoryGate's
`/runtime/library` page as Pi passes it on, without the records of a hidden chat), memory
corrections (pi.memory.edits), forgetting one or several memories (pi.memory.forget) and adding
one by hand (pi.memory.stated). The routes describe them with `pi.core.models.described` and do
not re-validate the reply.
"""

from typing import Literal

from pi.core.models import Strict

MemoryObjectKind = Literal[
    "memory", "entity", "evidence", "analysis", "episode", "observation", "pattern", "transcript"
]


class MemoryLibraryCard(Strict):
    """One record of MemoryGate's library page (its `memory_explorer.card` plus `connections`).

    The keys from `memory_type` to `valid_until` come only on a memory, `hypothesis` and
    `hypothesis_confidence` only on an observation. `confidence` is a word on a memory and a
    number on an analysis, episode or pattern; records without one send null."""

    type: MemoryObjectKind
    id: str
    preview: str
    preview_truncated: bool
    title: str
    status: str | None
    confidence: str | float | None
    available_fields: list[str]
    memory_type: str | None = None
    source_type: str | None = None
    do_not_generalize: bool | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    hypothesis: str | None = None
    hypothesis_confidence: float | None = None
    connections: dict[str, int]


class MemoryLibraryPage(Strict):
    """`total` counts the whole scope, less this page's records from a hidden chat."""

    objects: list[MemoryLibraryCard]
    total: int
    scope: Literal["all"]
    next_after: str | None
    search_mode: str
    instruction: str


class MemoryCorrectionWaiting(Strict):
    requestId: str
    memoryId: str
    expectedRevision: int
    payloadHash: str
    status: Literal["applying", "unknown"]


class MemoryCorrectionPending(Strict):
    """The one correction whose outcome is not settled yet, or null."""

    schemaVersion: Literal[1]
    pending: MemoryCorrectionWaiting | None


class MemoryCorrectionPreview(Strict):
    """The exact current text and revision of a memory the owner is about to correct."""

    schemaVersion: Literal[1]
    memoryId: str
    revision: int
    text: str
    sourceType: str
    confidence: str


class MemoryCorrectionStatus(Strict):
    """One correction; `revision` and `indexing` come from MemoryGate's receipt once applied."""

    schemaVersion: Literal[1]
    requestId: str
    memoryId: str
    previousRevision: int
    payloadHash: str
    status: Literal["applying", "unknown", "applied", "rejected"]
    revision: int | None
    indexing: Literal["pending", "indexed", "degraded"] | None
    errorCode: str | None
    cachedPackagesCleared: int
    sourceConversationKept: Literal[True]


class MemoryForgetWaiting(Strict):
    requestId: str
    memoryId: str
    revision: int


class MemoryForgetPending(Strict):
    """The forget request whose outcome is not settled yet (recall is paused), or null."""

    schemaVersion: Literal[1]
    pending: MemoryForgetWaiting | None


class MemoryForgetReceipt(Strict):
    """`indexRemoval` is MemoryGate's word; receipts stored before it was checked may hold any."""

    requestId: str
    memoryId: str
    status: Literal["forgotten"]
    indexRemoval: str | None
    cachedPackagesCleared: int
    sourceConversationKept: Literal[True]


class MemoryForgetStop(Strict):
    """Where a batch stopped: the item's position, its memory and the refusal."""

    index: int
    memoryId: str
    code: str
    message: str


class MemoryForgetManyResult(Strict):
    requestId: str
    results: list[MemoryForgetReceipt]
    stopped: MemoryForgetStop | None


class MemoryRemembered(Strict):
    memoryId: str
    status: Literal["remembered", "already_remembered"]
