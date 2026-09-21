"""Revisioned owner-authored character packages; preferences never imply synthesis support."""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import time
import zlib
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from . import agents

MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_HISTORY_BYTES = 128 * 1024 * 1024
MAX_VERSIONS = 100
Notes = Annotated[str, Field(max_length=6000)]
AssetId = Annotated[
    str, StringConstraints(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
]
Assignment = Annotated[str, Field(max_length=80)] | None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _media(source, kind):
    """Bounded signature/container checks only; never execute, decode frames, or fetch."""
    if kind == "image" and source == "/conker.png":
        return
    match = re.fullmatch(r"data:([a-z0-9/-]+);base64,([A-Za-z0-9+/=]+)", source)
    allowed = {
        "image": {"image/png", "image/jpeg", "image/webp"},
        "video": {"video/mp4", "video/webm"},
        "audio": {
            "audio/wav",
            "audio/x-wav",
            "audio/mpeg",
            "audio/mp3",
            "audio/ogg",
            "audio/webm",
            "audio/mp4",
        },
    }
    if not match or match[1] not in allowed[kind]:
        raise ValueError("Use an allowed embedded media type; external URLs are not accepted.")
    limit = (2 if kind == "image" else 8) * 1024 * 1024
    if len(match[2]) > ((limit + 2) // 3) * 4:
        raise ValueError("Embedded media exceeds its byte limit.")
    try:
        raw = base64.b64decode(match[2], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Embedded media is not valid base64.") from exc
    if not raw or len(raw) > limit:
        raise ValueError("Embedded media is empty or too large.")
    mime = match[1]
    if mime == "image/png":
        if raw[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("PNG contents do not match their media type.")
        position, names = 8, []
        while position + 12 <= len(raw):
            size = int.from_bytes(raw[position : position + 4], "big")
            end = position + 12 + size
            name = raw[position + 4 : position + 8]
            if end > len(raw) or name == b"acTL":
                raise ValueError("Use a bounded still PNG, not an animation.")
            if zlib.crc32(raw[position + 4 : end - 4]) != int.from_bytes(raw[end - 4 : end], "big"):
                raise ValueError("PNG checksum mismatch.")
            names.append(name)
            position = end
            if name == b"IEND":
                break
        if (
            position != len(raw)
            or not names
            or names[0] != b"IHDR"
            or names[-1] != b"IEND"
            or b"IDAT" not in names
        ):
            raise ValueError("PNG container is incomplete.")
    elif mime == "image/jpeg":
        if not raw.startswith(b"\xff\xd8\xff") or not raw.endswith(b"\xff\xd9"):
            raise ValueError("JPEG contents do not match their media type.")
    elif mime == "image/webp":
        if (
            len(raw) < 20
            or raw[:4] != b"RIFF"
            or raw[8:12] != b"WEBP"
            or int.from_bytes(raw[4:8], "little") + 8 != len(raw)
        ):
            raise ValueError("WebP container is invalid.")
        position, names = 12, []
        while position + 8 <= len(raw):
            name = raw[position : position + 4]
            size = int.from_bytes(raw[position + 4 : position + 8], "little")
            if name in (b"ANIM", b"ANMF"):
                raise ValueError("Use a still WebP image.")
            names.append(name)
            position += 8 + size + size % 2
        if position != len(raw) or not any(n in (b"VP8 ", b"VP8L") for n in names):
            raise ValueError("WebP container is incomplete.")
    elif mime in ("video/mp4", "audio/mp4"):
        if (
            len(raw) < 12
            or raw[4:8] != b"ftyp"
            or not 12 <= int.from_bytes(raw[:4], "big") <= len(raw)
        ):
            raise ValueError("MP4 contents do not match their media type.")
    elif mime in ("audio/webm", "video/webm"):
        if not raw.startswith(b"\x1aE\xdf\xa3"):
            raise ValueError("WebM contents do not match their media type.")
    elif mime in ("audio/wav", "audio/x-wav"):
        if (
            len(raw) < 12
            or raw[:4] != b"RIFF"
            or raw[8:12] != b"WAVE"
            or int.from_bytes(raw[4:8], "little") + 8 != len(raw)
        ):
            raise ValueError("WAV container is invalid.")
    elif mime == "audio/ogg":
        if not raw.startswith(b"OggS\x00"):
            raise ValueError("OGG contents do not match their media type.")
    elif not (raw.startswith(b"ID3") or (len(raw) >= 2 and raw[0] == 255 and raw[1] & 224 == 224)):
        raise ValueError("MP3 contents do not match their media type.")


def _identifier(value):
    if value in ("main", "neutral"):
        raise ValueError("Choose a unique asset identifier.")
    return value


class Detail(Strict):
    id: AssetId
    label: str = Field(max_length=80)
    value: str = Field(max_length=500)
    _id = field_validator("id")(_identifier)


class Examples(Strict):
    prompt: str = Field(max_length=1000)
    focus: Notes
    character: Notes


class Asset(Strict):
    id: AssetId
    name: str = Field(max_length=120)
    kind: Literal["image", "video"]
    src: str = Field(max_length=12 * 1024 * 1024)
    _id = field_validator("id")(_identifier)

    @model_validator(mode="after")
    def media(self):
        _media(self.src, self.kind)
        return self


class Activities(Strict):
    idle: Assignment
    listening: Assignment
    thinking: Assignment
    speaking: Assignment


class Expression(Strict):
    id: AssetId
    name: str = Field(min_length=1, max_length=60)
    instruction: str = Field(max_length=500)
    assetId: Assignment
    _id = field_validator("id")(_identifier)


class Appearance(Strict):
    description: Notes
    inset: float = Field(ge=12.5, le=25, allow_inf_nan=False)
    assets: list[Asset] = Field(max_length=16)
    activities: Activities
    expressions: list[Expression] = Field(max_length=16)

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


class Reference(Strict):
    name: str = Field(max_length=120)
    src: str = Field(max_length=12 * 1024 * 1024)

    @field_validator("src")
    @classmethod
    def audio(cls, value):
        _media(value, "audio")
        return value


class Voice(Strict):
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
    reference: Reference | None


class Mode(Strict):
    text: Notes
    voice: Notes
    expressiveness: float = Field(ge=0, le=100, allow_inf_nan=False)
    motion: bool


class Modes(Strict):
    default: Literal["focus", "character"]
    focus: Mode
    character: Mode


class Studio(Strict):
    soul: Notes
    backstory: Notes
    relationship: Notes
    details: list[Detail] = Field(max_length=20)
    examples: Examples
    appearance: Appearance
    voice: Voice
    modes: Modes

    @field_validator("details")
    @classmethod
    def unique_details(cls, value):
        if len({item.id for item in value}) != len(value):
            raise ValueError("Profile detail IDs must be unique.")
        return value


Face = Literal["sprout", "round", "cat"]
EmotionAppearance = Literal["default", "sprout", "round", "cat", "portrait"]


class Emotions(Strict):
    neutral: EmotionAppearance
    happy: EmotionAppearance
    thinking: EmotionAppearance
    concerned: EmotionAppearance
    celebrating: EmotionAppearance


class Profile(Strict):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    mood: str = Field(max_length=100)
    personality: Notes
    speakingStyle: Notes
    speakingPreset: Literal["warm", "direct", "curious", "custom"]
    portrait: str = Field(max_length=3 * 1024 * 1024)
    renderer: Literal["static", "live-2d", "live-3d"]
    face: Face
    tone: Literal["green", "soft", "graphite"]
    emotions: Emotions
    studio: Studio

    @field_validator("portrait")
    @classmethod
    def image(cls, value):
        if value:
            _media(value, "image")
        return value


class Save(Strict):
    expected_revision: int = Field(ge=0)
    profile: Profile


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


class CharacterError(RuntimeError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.status, self.detail = status, {"code": code, "message": message}


def _serialize(profile):
    value = Profile.model_validate(
        profile.model_dump() if isinstance(profile, Profile) else profile
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
