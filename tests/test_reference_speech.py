import base64
import copy
import json

import pytest
from test_calls import runtime, send, start
from test_character_speech import client, presentation, store  # noqa: F401
from test_characters import save
from test_speech import wav

from pi.speech import SpeechError


def reference(seconds=0.5):
    return {
        "name": "My voice",
        "src": "data:audio/wav;base64," + base64.b64encode(wav(seconds)).decode("ascii"),
    }


@pytest.mark.parametrize("transcript", ["Reference words", ""])
def test_reference_adapter_uses_bytes_without_voice_fallback(transcript):
    seen, value = [], presentation()
    value["character"]["profile"]["studio"]["voice"].update(
        source="reference", transcript=transcript
    )
    service = client(seen, "qwen3-base")
    result = service.synthesize("Answer", presentation=value, reference=reference())
    assert result["audio"] == wav()
    payload = seen[0]
    assert payload["task_type"] == "Base" and payload["ref_audio"] == reference()["src"]
    assert payload["x_vector_only_mode"] == (not bool(transcript))
    assert payload["ref_text"] == (transcript or None)
    assert "voice" not in payload and "instructions" not in payload


@pytest.mark.parametrize(
    "ref",
    [
        None,
        {"name": "remote", "src": "https://example.test/voice.wav"},
        {"name": "local", "src": "file:///private.wav"},
        {"name": "broken", "src": "data:audio/wav;base64,YmFk"},
    ],
)
def test_invalid_references_never_contact_server(ref):
    seen, value = [], presentation()
    value["character"]["profile"]["studio"]["voice"]["source"] = "reference"
    with pytest.raises(SpeechError):
        client(seen, "qwen3-base").synthesize("Answer", presentation=value, reference=ref)
    assert not seen


def test_call_reference_is_frozen_transient_and_not_in_model_context(request):
    database = request.getfixturevalue("store")
    value = presentation()["character"]["profile"]
    value["studio"]["voice"].update(
        source="reference", reference=reference(), transcript="Original"
    )
    save(database, value=value)
    call, seen = start(database), []
    loop, provider = runtime(database)

    def edit():
        updated = copy.deepcopy(value)
        updated["studio"]["voice"].update(reference=reference(0.25), transcript="Edited")
        save(database, 1, updated)

    provider.callback = edit
    service = client(seen, "qwen3-base")
    result = send(database, loop, call, speech=service)
    assert result["audio"] is not None
    assert seen[0]["ref_audio"] == reference()["src"] and seen[0]["ref_text"] == "Original"
    with database._connect() as db:
        preferences = db.execute("SELECT preferences FROM call_requests").fetchone()[0]
    assert "data:audio" not in preferences and "data:audio" not in str(provider.seen)
    assert "data:audio" not in json.dumps(result["call"])
    send(database, loop, call, speech=service)
    assert len(seen) == 1
    provider.callback = None
    send(database, loop, call, number=2, speech=service)
    assert seen[1]["ref_audio"] == reference(0.25)["src"] and seen[1]["ref_text"] == "Edited"
