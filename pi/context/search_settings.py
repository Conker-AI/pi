"""Owner search settings."""

from fastapi import HTTPException
from pydantic import Field

from pi.core.models import Strict

SOURCES = (
    "conversations",
    "memory",
    "projects",
    "artifacts",
    "tasks",
    "jobs",
    "agents",
    "tools",
    "activity",
)


class Settings(Strict):
    sources: list[str] = Field(default_factory=lambda: list(SOURCES), max_length=len(SOURCES))
    exactText: bool = True
    semantic: bool = False
    reranking: bool = False


class SearchSettingsDocument(Strict):
    revision: int
    configuration: Settings


class SaveSettings(Strict):
    expected_revision: int = Field(ge=0)
    configuration: Settings


def load_settings(store):
    with store._connect() as db:
        row = db.execute(
            "SELECT revision,configuration FROM owner_search_settings WHERE singleton=1"
        ).fetchone()
        return {
            "revision": row[0] if row else 0,
            "configuration": Settings.model_validate_json(row[1]).model_dump()
            if row
            else Settings().model_dump(),
        }


def save_settings(store, body, *, semantic_available, ranking_available):
    cfg = body.configuration
    if len(set(cfg.sources)) != len(cfg.sources) or not set(cfg.sources).issubset(SOURCES):
        raise HTTPException(422, "Choose unique supported search sources.")
    if cfg.semantic and not semantic_available:
        raise HTTPException(422, "Semantic memory retrieval is not available.")
    if cfg.reranking and not ranking_available:
        raise HTTPException(422, "Configure and enable the search-ranking model role first.")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT revision FROM owner_search_settings WHERE singleton=1").fetchone()
        revision = row[0] if row else 0
        if revision != body.expected_revision:
            raise HTTPException(409, "Search settings changed; reload before saving.")
        db.execute(
            "INSERT INTO owner_search_settings VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision,configuration=excluded.configuration",
            (revision + 1, cfg.model_dump_json()),
        )
        db.commit()
    return {"revision": revision + 1, "configuration": cfg.model_dump()}
