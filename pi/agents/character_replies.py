"""What the character routes send, named so OpenAPI describes each reply (D-01). The routes only
describe these: their replies are the dicts pi.agents.characters builds, sent unchanged."""

from typing import Literal

from pi.agents.characters import CharacterProfile
from pi.core.models import Strict


class CharacterRecord(Strict):
    """GET /characters/{agent_id}: the current or a chosen revision; revision 0 and no profile
    before the first save."""

    agentId: str
    revision: int
    profile: CharacterProfile | None


class CharacterSaved(Strict):
    """A save or restore: the new revision with the profile it holds."""

    agentId: str
    revision: int
    profile: CharacterProfile


class CharacterVersion(Strict):
    revision: int
    created_at: float
    restored_from: int | None


class CharacterHistory(Strict):
    results: list[CharacterVersion]


class CharacterPackage(Strict):
    """GET /characters/{agent_id}/export: a Conker v1 character package."""

    format: Literal["conker-character"]
    version: Literal[1]
    character: CharacterProfile


class CharacterImportDraft(Strict):
    """POST /characters/{agent_id}/import: a draft to review; nothing is saved."""

    profile: CharacterProfile
    note: str
