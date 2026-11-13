"""The replies of Pi's owner import routes (`/imports`), described in OpenAPI (D-01).

They name what pi.memory.imports, import_offers and import_deletion already send; the routes
describe them with `pi.core.models.described` and do not re-validate the reply. `source` is
closed: imported chats carry a CHECK on it and an import's source is the route's own Literal.
"""

from typing import Literal

from pi.core.models import Strict

ImportSource = Literal["chatgpt", "claude"]


class ImportSkipCounts(Strict):
    """What the export held that was left out or shortened (import_formats.SKIP_REASONS)."""

    chatsNotMatching: int
    duplicateChats: int
    emptyChats: int
    longChats: int
    toolOrHiddenMessages: int
    emptyMessages: int
    duplicateMessages: int
    shortenedMessages: int


class ImportMemoryChoice(Strict):
    """Whether imported words were offered to memory; `reason` says why not when `on` is off."""

    on: bool
    reason: str | None
    configured: bool


class ImportChatCounts(Strict):
    new: int
    updated: int
    unchanged: int


class ImportMessageCounts(Strict):
    added: int
    offeredToMemory: int


class ImportBatchResult(Strict):
    """One import as it was reported, also kept as a line of the import history."""

    schemaVersion: Literal[1]
    batchId: str
    source: ImportSource
    label: str
    digest: str
    importedAt: float
    chats: ImportChatCounts
    messages: ImportMessageCounts
    skipped: ImportSkipCounts
    memory: ImportMemoryChoice


class ImportMemoryStatus(Strict):
    """Imported words waiting for, sent to or refused by MemoryGate, and erasures still owed."""

    configured: bool
    waiting: int
    offered: int
    refused: int
    erasing: int
    notFoundToErase: int
    problem: str | None


class ImportHistory(Strict):
    schemaVersion: Literal[1]
    imports: list[ImportBatchResult]
    memory: ImportMemoryStatus


class ImportedChatSummary(Strict):
    id: str
    source: ImportSource
    label: str
    title: str
    exportId: str
    startedAt: float | None
    updatedAt: float | None
    importedAt: float
    messageCount: int
    archived: Literal[True]
    readOnly: Literal[True]


class ImportedChatList(Strict):
    schemaVersion: Literal[1]
    total: int
    chats: list[ImportedChatSummary]
    nextOffset: int | None


class ImportedMessage(Strict):
    """`memory` is None for the other assistant's replies, which are never offered."""

    id: str
    seq: int
    author: Literal["user", "assistant"]
    text: str
    createdAt: float | None
    shortened: bool
    exportId: str
    memory: Literal["offering", "offered", "refused", "not_offered"] | None


class ImportedChatTranscript(ImportedChatSummary):
    schemaVersion: Literal[1]
    messages: list[ImportedMessage]
    nextAfter: int | None


class ImportedChatDeletion(Strict):
    """`fileScrubbed` is None when this repeats an earlier deletion's record."""

    schemaVersion: Literal[1]
    chatId: str
    source: ImportSource
    label: str
    deletedAt: float
    messagesDeleted: int
    offeredToMemory: int
    memoryNote: str | None
    fileScrubbed: bool | None
