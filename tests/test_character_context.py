"""Presentation is frozen owner-authored context, not capability or factual evidence."""

import json
from contextlib import closing

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_characters import profile, save

from pi import calls, character_context, session_settings, session_settings_api
from pi.loop import Loop
from pi.providers import Completion
from pi.routing import Router
from pi.store import Store


def snapshot():
    return {
        "revision": 3,
        "profile": {
            "name": "Conker",
            "personality": "Ask critical questions.",
            "speakingStyle": "Use short sentences.",
            "studio": {
                "soul": "Be curious.",
                "backstory": "Fictional archivist from the moon.",
                "relationship": "A friendly collaborator.",
                "details": [{"id": "hobby", "label": "Hobby", "value": "Maps"}],
                "modes": {
                    "default": "character",
                    "focus": {"text": "Answer directly."},
                    "character": {"text": "Express the authored character."},
                },
            },
        },
    }


def test_focus_excludes_character_lore_and_personality():
    execution = {"character": snapshot(), "presentationMode": "focus"}
    message = character_context.messages(execution)[0]
    payload = json.loads(message.content.split("\n", 1)[1])
    assert payload == {"name": "Conker", "mode": "focus", "textStyle": "Answer directly."}
    assert "moon" not in message.content
    assert "Ask critical questions" not in message.content
    assert "not as evidence" in message.content


def test_character_includes_authored_fields_without_permissions():
    message = character_context.messages({"character": snapshot()})[0]
    payload = json.loads(message.content.split("\n", 1)[1])
    assert payload["mode"] == "character"
    assert payload["backstory"] == "Fictional archivist from the moon."
    assert payload["personality"] == "Ask critical questions."
    assert "permission" in message.content and "detected user emotions" in message.content


def test_absent_profile_does_not_invent_personality():
    assert character_context.messages({}) == []
    assert character_context.mode({}) == "focus"


class Provider:
    name = "local"

    def __init__(self):
        self.seen = []

    def complete(self, messages, *, model):
        self.seen.append(messages)
        return Completion("Answer", model, self.name)


def test_actual_turn_uses_frozen_profile_after_edit(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "character.db")) as store:
        save(store)
        session = store.create_session()
        provider = Provider()
        loop = Loop(store, Router(local_provider=provider, local_model="test"))
        bound = loop._run_bound

        def edit_then_run(*args, **kwargs):
            save(store, 1, profile("Changed after acceptance"))
            return bound(*args, **kwargs)

        monkeypatch.setattr(loop, "_run_bound", edit_then_run)
        first = loop.run_turn(session, "Hello", request_id="frozen_character_01")
        frozen = session_settings.execution(store, session, first["turn_id"])
        assert frozen["character"]["revision"] == 1
        assert "data:image" not in json.dumps(frozen)
        assert '"name": "Conker"' in provider.seen[0][0].content
        assert "Changed after acceptance" not in str(provider.seen[0])
        app = FastAPI()
        app.include_router(session_settings_api.router(lambda: store, lambda: None))
        with TestClient(app) as client:
            inspected = client.get(f"/sessions/{session}/settings/turns/{first['turn_id']}")
            assert inspected.status_code == 200
            assert inspected.json()["character"]["revision"] == 1

        monkeypatch.setattr(loop, "_run_bound", bound)
        loop.run_turn(session, "Again", request_id="frozen_character_02")
        assert "Changed after acceptance" in provider.seen[1][0].content


def test_call_focus_and_character_are_frozen_before_transcription(tmp_path):
    with closing(Store(tmp_path / "character.db")) as store:
        value = profile()
        value["studio"]["modes"]["default"] = "focus"
        save(store, value=value)
        session = store.create_session()
        call = calls.start(
            store,
            calls.Start(
                request_id="character_call_start",
                conversationId=session,
            ),
        )
        assert call["mode"] == "focus"
        calls.update(
            store,
            call["id"],
            calls.Update(
                expected_revision=1,
                channels=calls.ChannelUpdate(microphone=True, voice=False),
            ),
        )
        provider = Provider()
        loop = Loop(store, Router(local_provider=provider, local_model="test"))

        class Speech:
            def transcribe(self, audio, mime):
                save(store, 1, profile("Edited while transcribing"))
                calls.update(
                    store,
                    call["id"],
                    calls.Update(
                        expected_revision=2,
                        mode="character",
                    ),
                )
                return {"text": "Hello"}

        first = calls.run(
            store,
            loop,
            call["id"],
            calls.Send(
                request_id="character_call_audio",
            ),
            audio=b"synthetic transport",
            speech=Speech(),
        )
        assert first["call"]["requests"][0]["textStatus"] == "complete"
        presentation = provider.seen[0][0].content
        assert '"mode": "focus"' in presentation
        assert "Authored history" not in presentation
        assert "Edited while transcribing" not in presentation
        calls.run(
            store,
            loop,
            call["id"],
            calls.Send(
                request_id="character_call_typed",
                text="Continue",
            ),
        )
        presentation = provider.seen[1][0].content
        assert '"mode": "character"' in presentation
        assert "Authored history" in presentation
        assert "Edited while transcribing" in presentation
