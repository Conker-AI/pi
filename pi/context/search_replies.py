"""The replies of Pi's owner search routes (`/search`), described in OpenAPI (D-01).

They name what pi.context.universal_search already sends; the routes describe them with
`pi.core.models.described` and do not re-validate the reply.
"""

from typing import Literal

from pi.core.models import Strict

Stage = Literal["metadata", "text", "semantic"]


class SearchCapabilities(Strict):
    sources: list[str]
    exactText: bool
    semanticSources: list[str]
    reranking: bool
    indexing: Literal["source-owned"]
    scanLimit: int


class SearchResult(Strict):
    """One record: `matchType` says how it matched (`text` when semantic memory fell back)."""

    id: str
    source: str
    recordId: str
    title: str
    excerpt: str
    href: str
    matchType: Stage
    role: str | None


class SearchCoverage(Strict):
    source: str
    status: Literal["searched", "partial", "degraded", "unavailable"]


class SearchRanking(Strict):
    """`modelId` and `candidates` only when the page was ranked."""

    status: Literal["disabled", "unavailable", "not_needed", "ranked", "fallback"]
    modelId: str | None = None
    candidates: int | None = None


class SearchPage(Strict):
    results: list[SearchResult]
    coverage: list[SearchCoverage]
    nextCursor: str | None
    stage: Stage
    ranking: SearchRanking
