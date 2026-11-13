"""The replies of the owner's skill routes (`/skills`), described in OpenAPI (D-01).

They name what `pi.agents.skills` already sends (`_view`); the routes describe them with
`pi.core.models.described` and do not re-validate the reply.
"""

from typing import Literal

from pi.core.models import Strict


class SkillSummary(Strict):
    """One skill in the registry; `revision` grows on every import or state change."""

    id: str
    name: str
    description: str
    revision: int
    enabled: bool
    sha256: str
    createdAt: float
    updatedAt: float


class SkillDetail(SkillSummary):
    """One skill with its SKILL.md text exactly as imported."""

    markdown: str


class SkillList(Strict):
    schemaVersion: Literal[1]
    results: list[SkillSummary]
