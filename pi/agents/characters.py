"""Revisioned owner-authored character packages; preferences never imply synthesis support."""

from __future__ import annotations

import copy
import json
import time
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from pi.agents import profiles as agents
from pi.agents.character_media import _media
from pi.core.errors import ServiceError
from pi.core.models import Strict

MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 66 * 1024 * 1024
MAX_HISTORY_BYTES = 128 * 1024 * 1024
MAX_VERSIONS = 100
Notes = Annotated[str, Field(max_length=6000)]
AssetId = Annotated[
    str, StringConstraints(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
]
Assignment = Annotated[str, Field(max_length=80)] | None


def _identifier(value):
    if value in ("main", "neutral"):
        raise ValueError("Choose a unique asset identifier.")
    return value


class CharacterDetail(Strict):
    id: AssetId
    label: str = Field(max_length=80)
    value: str = Field(max_length=500)
    _id = field_validator("id")(_identifier)


class CharacterExamples(Strict):
    prompt: str = Field(max_length=1000)
    focus: Notes
    character: Notes


class CharacterAsset(Strict):
    id: AssetId
    name: str = Field(max_length=120)
    kind: Literal["image", "video"]
    src: str = Field(max_length=12 * 1024 * 1024)
    _id = field_validator("id")(_identifier)

    @model_validator(mode="after")
    def media(self):
        _media(self.src, self.kind)
        return self


class CharacterActivities(Strict):
    idle: Assignment
    listening: Assignment
    thinking: Assignment
    speaking: Assignment


class CharacterExpression(Strict):
    id: AssetId
    name: str = Field(min_length=1, max_length=60)
    instruction: str = Field(max_length=500)
    assetId: Assignment
    _id = field_validator("id")(_identifier)


class CharacterAppearance(Strict):
    description: Notes
    inset: float = Field(ge=12.5, le=25, allow_inf_nan=False)
    assets: list[CharacterAsset] = Field(max_length=16)
    activities: CharacterActivities
    expressions: list[CharacterExpression] = Field(max_length=16)

    @model_validator(mode="after")
    def references(self):
        ids = {a.id for a in self.assets}
        if len(ids) != len(self.assets) or len({e.id for e in self.expressions}) != len(
            self.expressions
        ):
            raise ValueError("Asset and expression IDs must be unique.")
        for identity in [
            *self.activities.model_dump().values(),
            *(e.assetId for e in self.expressions),
        ]:
            if identity and identity not in ids:
                raise ValueError("An appearance assignment refers to a missing asset.")
        return self


class CharacterVoiceReference(Strict):
    name: str = Field(max_length=120)
    src: str = Field(max_length=12 * 1024 * 1024)

    @field_validator("src")
    @classmethod
    def audio(cls, value):
        _media(value, "audio")
        return value


class CharacterVoice(Strict):
    source: Literal["design", "reference"]
    engine: Literal["qwen3-tts"]
    description: Notes
    language: Literal[
        "English",
        "Chinese",
        "Japanese",
        "Korean",
        "German",
        "French",
        "Russian",
        "Portuguese",
        "Spanish",
        "Italian",
    ]
    pronunciation: Notes
    transcript: str = Field(max_length=3000)
    reference: CharacterVoiceReference | None


class CharacterMode(Strict):
    text: Notes
    voice: Notes
    expressiveness: float = Field(ge=0, le=100, allow_inf_nan=False)
    motion: bool


class CharacterModes(Strict):
    default: Literal["focus", "character"]
    focus: CharacterMode
    character: CharacterMode


class CharacterStudio(Strict):
    soul: Notes
    backstory: Notes
    relationship: Notes
    details: list[CharacterDetail] = Field(max_length=20)
    examples: CharacterExamples
    appearance: CharacterAppearance
    voice: CharacterVoice
    modes: CharacterModes

    @field_validator("details")
    @classmethod
    def unique_details(cls, value):
        if len({item.id for item in value}) != len(value):
            raise ValueError("Profile detail IDs must be unique.")
        return value


Face = Literal["sprout", "round", "cat"]
EmotionAppearance = Literal["default", "sprout", "round", "cat", "portrait"]


class CharacterEmotions(Strict):
    neutral: EmotionAppearance
    happy: EmotionAppearance
    thinking: EmotionAppearance
    concerned: EmotionAppearance
    celebrating: EmotionAppearance


class CharacterProfile(Strict):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    mood: str = Field(max_length=100)
    personality: Notes
    speakingStyle: Notes
    speakingPreset: Literal["warm", "direct", "curious", "custom"]
    portrait: str = Field(max_length=3 * 1024 * 1024)
    renderer: Literal["static", "live-2d", "live-3d"]
    face: Face
    tone: Literal["green", "soft", "graphite"]
    emotions: CharacterEmotions
    studio: CharacterStudio

    @field_validator("portrait")
    @classmethod
    def image(cls, value):
        if value:
            _media(value, "image")
        return value


# Short names kept for callers; the Character* names are what OpenAPI shows (D-01).
Detail = CharacterDetail
Examples = CharacterExamples
Asset = CharacterAsset
Activities = CharacterActivities
Expression = CharacterExpression
Appearance = CharacterAppearance
Reference = CharacterVoiceReference
Voice = CharacterVoice
Mode = CharacterMode
Modes = CharacterModes
Studio = CharacterStudio
Emotions = CharacterEmotions
Profile = CharacterProfile


class Save(Strict):
    expected_revision: int = Field(ge=0)
    profile: CharacterProfile


class Restore(Strict):
    expected_revision: int = Field(ge=1)
    revision: int = Field(ge=1)


class Import(Strict):
    text: str = Field(max_length=MAX_PACKAGE_BYTES)


SCHEMA = """
CREATE TABLE IF NOT EXISTS character_profiles (
 agent_id TEXT PRIMARY KEY REFERENCES agents(id), revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS character_versions (
 agent_id TEXT NOT NULL REFERENCES agents(id), revision INTEGER NOT NULL,
 profile TEXT NOT NULL, created_at REAL NOT NULL, restored_from INTEGER,
 PRIMARY KEY(agent_id,revision)
);
CREATE TRIGGER IF NOT EXISTS character_versions_no_replace BEFORE INSERT ON character_versions
WHEN EXISTS(SELECT 1 FROM character_versions WHERE agent_id=NEW.agent_id AND revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'character versions are immutable'); END;
"""
for _operation in ("UPDATE", "DELETE"):
    SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS character_versions_no_{_operation.lower()}
BEFORE {_operation} ON character_versions
BEGIN SELECT RAISE(ABORT,'character versions are immutable'); END;
"""


class CharacterError(ServiceError, RuntimeError):
    pass


def _serialize(profile):
    value = CharacterProfile.model_validate(
        profile.model_dump() if isinstance(profile, CharacterProfile) else profile
    )
    # The frontend editor normalizes all authored drafts to custom; legacy labels grant no behavior.
    value.speakingPreset = "custom"
    encoded = value.model_dump_json()
    if len(encoded.encode("utf-8")) > MAX_PACKAGE_BYTES:
        raise CharacterError("package_limit", "Keep the character package within 32 MiB.", 413)
    return encoded


def _agent(db, identity, *, editing=False):
    value = agents._get(db, identity)
    if editing and value["archived_at"] is not None:
        raise CharacterError("agent_archived", "Restore the agent before editing its character.")
    return value


def snapshot(db, agent_id):
    row = db.execute(
        "SELECT v.revision,v.profile FROM character_profiles p "
        "JOIN character_versions v ON v.agent_id=p.agent_id "
        "AND v.revision=p.revision WHERE p.agent_id=?",
        (agent_id,),
    ).fetchone()
    return {"revision": row[0], "profile": json.loads(row[1])} if row else None


def runtime_snapshot(db, agent_id):
    value = snapshot(db, agent_id)
    if value is None:
        return None
    source = value["profile"]
    studio = source["studio"]
    voice = copy.deepcopy(studio["voice"])
    if voice["reference"]:
        voice["reference"] = {"name": voice["reference"]["name"], "present": True}
    return {
        "revision": value["revision"],
        "profile": {
            "name": source["name"],
            "personality": source["personality"],
            "speakingStyle": source["speakingStyle"],
            "studio": {
                key: copy.deepcopy(studio[key])
                for key in ("soul", "backstory", "relationship", "details", "modes")
            }
            | {"voice": voice},
        },
    }


def get(store, agent_id, revision=None):
    with store._connect() as db:
        db.execute("BEGIN")
        _agent(db, agent_id)
        if revision is None:
            value = snapshot(db, agent_id)
        else:
            row = db.execute(
                "SELECT profile FROM character_versions WHERE agent_id=? AND revision=?",
                (agent_id, revision),
            ).fetchone()
            if row is None:
                raise CharacterError("not_found", "Character version not found.", 404)
            value = {"revision": revision, "profile": json.loads(row[0])}
        return (
            {"agentId": agent_id, **value}
            if value
            else {"agentId": agent_id, "revision": 0, "profile": None}
        )


def history(store, agent_id):
    with store._connect() as db:
        _agent(db, agent_id)
        return [
            dict(row)
            for row in db.execute(
                "SELECT revision,created_at,restored_from FROM character_versions "
                "WHERE agent_id=? ORDER BY revision",
                (agent_id,),
            )
        ]


def _append(db, agent_id, expected, encoded, restored=None):
    current = db.execute(
        "SELECT revision FROM character_profiles WHERE agent_id=?", (agent_id,)
    ).fetchone()
    revision = current[0] if current else 0
    if revision != expected:
        raise CharacterError("revision_conflict", "Character changed; reload before saving.")
    total = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(length(CAST(profile AS BLOB))),0) FROM character_versions "
        "WHERE agent_id=?",
        (agent_id,),
    ).fetchone()
    if total[0] >= MAX_VERSIONS or total[1] + len(encoded.encode()) > MAX_HISTORY_BYTES:
        raise CharacterError(
            "history_limit", "Character history reached its 100-version/128-MiB limit.", 413
        )
    db.execute(
        "INSERT INTO character_versions VALUES(?,?,?,?,?)",
        (agent_id, revision + 1, encoded, time.time(), restored),
    )
    db.execute(
        "INSERT INTO character_profiles VALUES(?,?) ON CONFLICT(agent_id) "
        "DO UPDATE SET revision=excluded.revision",
        (agent_id, revision + 1),
    )
    return {"agentId": agent_id, "revision": revision + 1, "profile": json.loads(encoded)}


def save(store, agent_id, body: Save):
    body = Save.model_validate(body.model_dump())
    encoded = _serialize(body.profile)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _agent(db, agent_id, editing=True)
        result = _append(db, agent_id, body.expected_revision, encoded)
        db.commit()
        return result


def restore(store, agent_id, body: Restore):
    body = Restore.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _agent(db, agent_id, editing=True)
        row = db.execute(
            "SELECT profile FROM character_versions WHERE agent_id=? AND revision=?",
            (agent_id, body.revision),
        ).fetchone()
        if row is None:
            raise CharacterError("not_found", "Character version not found.", 404)
        result = _append(db, agent_id, body.expected_revision, row[0], body.revision)
        db.commit()
        return result


def export(store, agent_id, revision=None):
    value = get(store, agent_id, revision)
    if value["profile"] is None:
        raise CharacterError("not_found", "Character profile not found.", 404)
    return {"format": "conker-character", "version": 1, "character": value["profile"]}


def import_draft(store, agent_id, body: Import):
    body = Import.model_validate(body.model_dump())
    with store._connect() as db:
        _agent(db, agent_id, editing=True)
    if len(body.text.encode()) > MAX_PACKAGE_BYTES:
        raise CharacterError("package_limit", "Character import exceeds 32 MiB.", 413)
    try:
        raw = json.loads(body.text)
    except (ValueError, RecursionError) as exc:
        raise CharacterError("invalid_package", "Choose valid character JSON.", 422) from exc
    if not isinstance(raw, dict):
        raise CharacterError("invalid_package", "Choose a character package.", 422)
    if (
        raw.get("format") == "conker-character"
        and type(raw.get("version")) is int
        and raw["version"] == 1
    ):
        with store._connect() as db:
            _agent(db, agent_id, editing=True)
        profile = json.loads(_serialize(raw.get("character")))
        return {
            "profile": profile,
            "note": "Imported into a draft. Review, then save with the current revision.",
        }
    if raw.get("spec") not in ("chara_card_v2", "chara_card_v3") or not isinstance(
        raw.get("data"), dict
    ):
        raise CharacterError("invalid_package", "Use Conker v1 or Character Card V2/V3 JSON.", 422)
    data = raw["data"]
    current = get(store, agent_id)
    if current["profile"] is None:
        raise CharacterError(
            "profile_required", "Save a full character profile before importing card text."
        )
    for key in ("name", "description", "personality", "scenario", "mes_example", "first_mes"):
        if key in data and (
            not isinstance(data[key], str) or len(data[key]) > (60 if key == "name" else 6000)
        ):
            raise CharacterError("invalid_card", "Card text fields exceed supported bounds.", 422)
    if not data.get("name"):
        raise CharacterError("invalid_card", "Character card requires a name.", 422)
    profile = current["profile"]
    profile.update(
        name=data["name"],
        personality=data.get("personality", ""),
        speakingStyle=data.get("mes_example", ""),
    )
    profile["studio"].update(
        backstory=data.get("description", ""), relationship=data.get("scenario", "")
    )
    profile["studio"]["examples"]["character"] = data.get("first_mes", "")
    return {
        "profile": json.loads(_serialize(profile)),
        "note": "Imported card text into a draft; artwork and voice retained. "
        "Card instructions and embedded packs were not imported.",
    }
