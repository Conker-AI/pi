"""Frozen call presentation through synthetic HTTP; no speech services or devices."""

import copy
import json

import httpx
import pytest
from test_calls import runtime, send, start
from test_characters import profile, save
from test_speech import wav

from pi import calls, speech
from pi.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "character-speech.db")
    yield value
    value.close()


def presentation(mode="focus"):
    value = profile()
    value["studio"]["voice"].update(description="Low, warm voice", pronunciation="Pi: pie")
    value["studio"]["modes"]["focus"].update(voice="Clear and steady", expressiveness=15)
    value["studio"]["modes"]["character"].update(voice="Lively storyteller", expressiveness=65)
    return {"mode": mode, "character": {"revision": 1, "profile": value}}


def client(seen, adapter="qwen3-design"):
    def handle(request):
        seen.append(json.loads(request.read()))
        return httpx.Response(200, content=wav(), headers={"content-type": "audio/wav"})

    return speech.SpeechClient(
        "http://speech.test/v1",
        tts_model="operator-model",
        voice="ordinary-voice",
        character_voice=adapter,
        transport=httpx.MockTransport(handle),
    )


def test_design_and_mode_instructions_reach_same_configured_server():
    seen = []
    service = client(seen)
    for mode in ("focus", "character"):
        assert service.synthesize("Exact answer", presentation=presentation(mode))["audio"] == wav()
    for payload in seen:
        assert payload["model"] == "operator-model"
        assert payload["input"] == "Exact answer"
        assert payload["task_type"] == "VoiceDesign"
        assert payload["language"] == "English"
        assert "voice" not in payload
        assert "Low, warm voice" in payload["instructions"]
        assert "Pi: pie" in payload["instructions"]
        assert "Authored history" not in payload["instructions"]
        assert "data:" not in json.dumps(payload)
    assert "Clear and steady" in seen[0]["instructions"]
    assert "15" in seen[0]["instructions"]
    assert "Lively storyteller" not in seen[0]["instructions"]
    assert "Lively storyteller" in seen[1]["instructions"]
    assert "65" in seen[1]["instructions"]
    assert service.capabilities()["character_voice"]["reference"] == "unsupported"
    assert service.capabilities()["emotion_control"] is False


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"source": "reference"}, "character_reference_unsupported"),
        ({"language": "French"}, "character_language_unsupported"),
        ({"description": " "}, "character_design_required"),
        ({"description": "bad\x00description"}, "invalid_character_voice"),
        ({"description": "bad\ud800description"}, "invalid_character_voice"),
    ],
)
def test_unsupported_or_invalid_settings_never_fall_back_or_contact_server(change, code):
    seen, value = [], presentation()
    value["character"]["profile"]["studio"]["voice"].update(change)
    with pytest.raises(speech.SpeechError) as error:
        client(seen).synthesize("Exact answer", presentation=value)
    assert error.value.code == code
    assert not seen


def test_generic_adapter_rejects_character_but_keeps_ordinary_voice():
    seen = []
    service = client(seen, "unsupported")
    with pytest.raises(speech.SpeechError) as error:
        service.synthesize("Answer", presentation=presentation())
    assert error.value.code == "character_voice_unsupported" and not seen
    service.synthesize("Answer")
    assert seen[0]["voice"] == "ordinary-voice" and "instructions" not in seen[0]


def test_call_freezes_voice_before_provider_and_applies_edits_to_next_request(store):
    value = presentation()["character"]["profile"]
    value["studio"]["modes"]["default"] = "focus"
    save(store, value=value)
    call = start(store)
    seen = []
    service = client(seen)
    loop, provider = runtime(store)

    def edit():
        changed = copy.deepcopy(value)
        changed["studio"]["voice"]["description"] = "New identity"
        save(store, 1, changed)
        calls.update(store, call["id"], calls.Update(expected_revision=1, mode="character"))

    provider.callback = edit
    result = send(store, loop, call, speech=service)
    assert result["call"]["requests"][0]["speechStatus"] == "generated-transient"
    assert "Low, warm voice" in seen[0]["instructions"]
    assert "Clear and steady" in seen[0]["instructions"]
    assert "New identity" not in seen[0]["instructions"]
    provider.callback = None
    send(store, loop, call, number=2, speech=service)
    assert "New identity" in seen[1]["instructions"]
    assert "Lively storyteller" in seen[1]["instructions"]
    send(store, loop, call, number=2, speech=service)
    assert len(seen) == 2


def test_call_records_voice_failure_without_losing_answer_or_replaying(store):
    save(store)
    call, seen = start(store), []
    loop, provider = runtime(store)
    result = send(store, loop, call, speech=client(seen, "unsupported"))
    receipt = result["call"]["requests"][0]
    assert receipt["errorCode"] == "character_voice_unsupported"
    assert receipt["state"] == receipt["textStatus"] == "complete"
    assert receipt["speechStatus"] == "failed-or-interrupted"
    assert result["audio"] is None and not seen
    send(store, loop, call, speech=client(seen))
    assert not seen and len(provider.seen) == 1


def test_call_interruption_during_design_synthesis_discards_audio(store):
    save(store)
    call = start(store)
    loop, _ = runtime(store)

    def stop(request):
        assert json.loads(request.read())["task_type"] == "VoiceDesign"
        calls.interrupt(store, call["id"], calls.Revision(expected_revision=1), ended=True)
        return httpx.Response(200, content=wav(), headers={"content-type": "audio/wav"})

    service = speech.SpeechClient(
        "http://speech.test/v1",
        tts_model="design-model",
        character_voice="qwen3-design",
        transport=httpx.MockTransport(stop),
    )
    result = send(store, loop, call, speech=service)
    assert result["audio"] is None
    assert result["call"]["requests"][0]["textStatus"] == "complete"
    assert result["call"]["requests"][0]["speechStatus"] == "failed-or-interrupted"
