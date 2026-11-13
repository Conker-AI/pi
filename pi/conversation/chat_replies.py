"""The replies of Pi's chat list, chat settings and new-chat draft routes, described in OpenAPI
(D-01).

They name what pi.conversation.archive, settings and drafts already send; the routes describe
them with `pi.core.models.described` and do not re-validate the reply.
"""

from typing import Literal

from pi.context.projects import Reference
from pi.core.models import Strict

ResearchMode = Literal["off", "web", "deep"]
PrivacyFlag = Literal["memoryDisabled", "harnessDisabled"]


class ChatArchiveResult(Strict):
    """POST /sessions/archive: the chats now in that state, each once, in the order sent."""

    sessions: list[str]
    archived: bool


class ChatTrashResult(Strict):
    """POST /sessions/trash: the chats moved to the Trash (`trashed`) or restored from it."""

    sessions: list[str]
    trashed: bool


class ChatSettingsPrivacy(Strict):
    memoryDisabled: bool
    harnessDisabled: bool


class ChatSettings(Strict):
    """A chat's saved settings (pi.conversation.settings.Settings); keys a chat was saved
    without are absent."""

    agentId: str
    privacy: ChatSettingsPrivacy
    projectId: str | None = None
    projectSources: list[Reference] | None = None
    presentationMode: Literal["focus", "character"] | None = None
    access: Literal["default", "ask", "ai", "full"] | None = None


class ChatSettingsDocument(Strict):
    """GET and POST /sessions/{identity}/settings: revision 0 until the first save. `settings`
    keeps the keys it was saved with, so a chat saved before a field existed omits it."""

    revision: int
    settings: ChatSettings


class NewChatDraftDocument(Strict):
    """GET and POST /drafts/new: the one draft of a chat not started yet."""

    schemaVersion: Literal[1]
    session_id: None
    task_id: None
    content_status: Literal["available"]
    revision: int
    updated_at: float | None
    text: str
    research_mode: ResearchMode
    model_id: str | None
    skill_ids: list[str]
    think: bool
    title: str
    agent_id: str | None
    privacy: dict[PrivacyFlag, bool]


class NewChatStartDocument(Strict):
    """POST /drafts/new/start and GET /drafts/new/start/{request_id}: the chat a start made.
    A forgotten chat keeps its id; its revisions are gone (null)."""

    schemaVersion: Literal[1]
    request_id: str
    session_id: str
    content_status: Literal["available", "forgotten"]
    source_revision: int | None
    draft_revision: int | None
