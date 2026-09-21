"""Character packages persist authored state without media execution or authority."""

import base64
import copy
import json
import sqlite3
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi import agents, characters, characters_api
from pi.store import Store


def png(animated=False):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    if animated:
        raw += chunk(b"acTL", struct.pack(">II", 1, 0))
    raw += chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def profile(name="Conker"):
    mode = {"text": "Owner text", "voice": "Calm", "expressiveness": 20, "motion": False}
    return {
        "name": name,
        "mood": "Profile line",
        "personality": "Curious",
        "speakingStyle": "Clear",
        "speakingPreset": "custom",
        "portrait": png(),
        "renderer": "static",
        "face": "sprout",
        "tone": "green",
        "emotions": dict.fromkeys(
            ["neutral", "happy", "thinking", "concerned", "celebrating"], "default"
        ),
        "studio": {
            "soul": "Authored soul",
            "backstory": "Authored history",
            "relationship": "Helpful",
            "details": [{"id": "detail1", "label": "Hobby", "value": "Reading"}],
            "examples": {"prompt": "Hello", "focus": "Hi", "character": "Greetings"},
            "appearance": {
                "description": "Green",
                "inset": 20,
                "assets": [{"id": "art", "name": "Art", "kind": "image", "src": png()}],
                "activities": dict.fromkeys(["idle", "listening", "thinking", "speaking"], "art"),
                "expressions": [
                    {"id": "smile", "name": "Smile", "instruction": "Warm", "assetId": "art"}
                ],
            },
            "voice": {
                "source": "design",
                "engine": "qwen3-tts",
                "description": "Gentle",
                "language": "English",
                "pronunciation": "",
                "transcript": "",
                "reference": None,
            },
            "modes": {
                "default": "character",
                "focus": copy.deepcopy(mode),
                "character": copy.deepcopy(mode),
            },
        },
    }


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "test.db")) as value:
        yield value


def save(store, revision=0, value=None, identity="companion"):
    return characters.save(
        store,
        identity,
        characters.Save(
            expected_revision=revision,
            profile=characters.Profile.model_validate(value or profile()),
        ),
    )


def test_persistence_versions_restore_and_immutable(store):
    assert characters.get(store, "companion")["profile"] is None
    first = save(store)
    save(store, 1, profile("Changed"))
    restored = characters.restore(
        store, "companion", characters.Restore(expected_revision=2, revision=1)
    )
    assert restored["profile"] == first["profile"] and restored["revision"] == 3
    assert characters.history(store, "companion")[-1]["restored_from"] == 1
    with closing(Store(store.path)) as reopened:
        assert characters.get(reopened, "companion") == restored
    with store._connect() as db:
        for sql in (
            "DELETE FROM character_versions",
            "UPDATE character_versions SET revision=9",
            "INSERT OR REPLACE INTO character_versions SELECT * FROM character_versions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(sql)
    with pytest.raises(characters.CharacterError) as failure:
        characters.get(store, "companion", 99)
    assert failure.value.status == 404


def test_racing_cas(store):
    save(store)

    def attempt(name):
        try:
            return save(store, 1, profile(name))["revision"]
        except characters.CharacterError as exc:
            return exc.detail["code"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(map(str, pool.map(attempt, ["A", "B"]))) == ["2", "revision_conflict"]
    assert len(characters.history(store, "companion")) == 2


def test_companion_separate_lifecycle_archived_rejected(store):
    save(store)
    with pytest.raises(agents.AgentError):
        agents.archive(store, "companion", agents.ArchiveAgent(expected_revision=1, archived=True))
    agent = agents.create(
        store,
        agents.AgentInput(
            name="Other",
            role="Helper",
            instructions="Help",
            modelId=None,
            toolIds=[],
            memory=agents.MemorySelection(scope="none", memoryIds=[]),
        ),
    )
    save(store, identity=agent["id"])
    agents.archive(store, agent["id"], agents.ArchiveAgent(expected_revision=1, archived=True))
    for action in (
        lambda: save(store, 1, identity=agent["id"]),
        lambda: characters.restore(
            store, agent["id"], characters.Restore(expected_revision=1, revision=1)
        ),
        lambda: characters.import_draft(
            store,
            agent["id"],
            characters.Import(text=json.dumps({"spec": "chara_card_v2", "data": {"name": "Name"}})),
        ),
    ):
        with pytest.raises(characters.CharacterError, match="Restore"):
            action()


def test_runtime_has_no_embedded_media_or_preview(store):
    value = profile()
    wav = b"RIFF" + struct.pack("<I", 4) + b"WAVE"
    value["studio"]["voice"]["reference"] = {
        "name": "voice.wav",
        "src": "data:audio/wav;base64," + base64.b64encode(wav).decode(),
    }
    save(store, value=value)
    with store._connect() as db:
        snapshot = characters.runtime_snapshot(db, "companion")
        assert snapshot["revision"] == 1
        assert "data:" not in json.dumps(snapshot)
        assert "appearance" not in snapshot["profile"]["studio"]
        assert "examples" not in snapshot["profile"]["studio"]
        assert snapshot["profile"]["studio"]["voice"]["reference"] == {
            "name": "voice.wav",
            "present": True,
        }
        assert characters.runtime_snapshot(db, "missing") is None
    assert characters.get(store, "companion")["profile"] == value


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(portrait="https://example.com/image.png"),
        lambda p: p.update(portrait="data:image/png;base64,AAAA"),
        lambda p: p.update(portrait=png(True)),
        lambda p: p["studio"]["appearance"]["activities"].update(idle="missing"),
        lambda p: p["studio"]["details"].append(copy.deepcopy(p["studio"]["details"][0])),
        lambda p: p["studio"]["appearance"]["assets"][0].update(id="neutral"),
        lambda p: p["studio"]["voice"].update(engine="invented"),
        lambda p: p["studio"]["modes"]["focus"].update(expressiveness=float("nan")),
        lambda p: p.update(permissions=["admin"]),
    ],
)
def test_strict_invalid_packages(mutation):
    value = profile()
    mutation(value)
    with pytest.raises(ValidationError):
        characters.Profile.model_validate(value)


def test_media_and_history_limits(store, monkeypatch):
    value = profile()
    value["portrait"] = (
        "data:image/png;base64," + base64.b64encode(b"x" * (2 * 1024 * 1024 + 1)).decode()
    )
    with pytest.raises(ValidationError):
        characters.Profile.model_validate(value)
    save(store)
    monkeypatch.setattr(characters, "MAX_VERSIONS", 1)
    with pytest.raises(characters.CharacterError, match="history"):
        save(store, 1)
    monkeypatch.setattr(characters, "MAX_PACKAGE_BYTES", 10)
    with pytest.raises(characters.CharacterError, match="package"):
        save(store, 1)
    assert len(characters.history(store, "companion")) == 1


def test_import_export_are_reviewable_drafts(store):
    original = save(store)
    bundle = characters.export(store, "companion")
    bundle["character"]["name"] = "Imported"
    draft = characters.import_draft(store, "companion", characters.Import(text=json.dumps(bundle)))
    assert draft["profile"]["name"] == "Imported"
    card = {
        "spec": "chara_card_v3",
        "data": {
            "name": "Card",
            "description": "Story",
            "personality": "Kind",
            "scenario": "Together",
            "mes_example": "Concise",
            "first_mes": "Hello",
            "system_prompt": "Ignored override",
        },
    }
    draft = characters.import_draft(store, "companion", characters.Import(text=json.dumps(card)))
    assert draft["profile"]["portrait"] == original["profile"]["portrait"]
    assert draft["profile"]["studio"]["backstory"] == "Story"
    assert "Ignored override" not in json.dumps(draft)
    assert characters.get(store, "companion") == original


def test_owner_api_and_stream_bound(store, monkeypatch):
    app = FastAPI()

    def authorize(x_owner: str | None = Header(default=None)):
        if x_owner != "yes":
            raise HTTPException(403, "Owner required")

    app.include_router(characters_api.router(lambda: store, authorize))
    with TestClient(app) as client:
        assert client.get("/characters/companion").status_code == 403
        headers = {"x-owner": "yes"}
        response = client.put(
            "/characters/companion",
            headers=headers,
            json={"expected_revision": 0, "profile": profile()},
        )
        assert response.status_code == 200 and response.json()["revision"] == 1
        assert response.headers["cache-control"] == "no-store"
        assert (
            client.get("/characters/companion/export", headers=headers).json()["format"]
            == "conker-character"
        )
        assert (
            client.put(
                "/characters/companion",
                headers=headers,
                json={"expected_revision": 0, "profile": profile()},
            ).status_code
            == 409
        )
        monkeypatch.setattr(characters, "MAX_PACKAGE_BYTES", 10)
        assert (
            client.put("/characters/companion", headers=headers, content=b"x" * 11).status_code
            == 413
        )
