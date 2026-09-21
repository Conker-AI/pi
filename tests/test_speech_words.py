import json

import httpx
import pytest
from test_speech import client, wav

from pi.speech import SpeechError


def test_word_timing_preserves_provider_values_and_requests_both_granularities():
    words = [{"start": 0.1, "end": 0.5, "word": "Hello"}]

    def handle(request):
        body = request.read()
        assert body.count(b'name="timestamp_granularities[]"') == 2
        assert b"\r\n\r\nword\r\n" in body
        return httpx.Response(200, json={"text": "Hello", "words": words})

    result = client(handle).transcribe(wav(), "audio/wav")
    assert result["words"] == words and result["segments"] is None


@pytest.mark.parametrize(
    "words",
    [
        [{"start": -0.1, "end": 0.5, "word": "hi"}],
        [{"start": 0, "end": 1.1, "word": "hi"}],
        [{"start": True, "end": 1, "word": "hi"}],
        [{"start": 0, "end": float("nan"), "word": "hi"}],
        [{"start": 0, "end": 0.7, "word": "hi"}, {"start": 0.6, "end": 1, "word": "there"}],
        [{"start": 0, "end": 1, "word": ""}],
        [{"start": 0, "end": 1, "word": "x" * 16001}],
        [{"start": 0, "end": 0, "word": "x"}] * 4001,
        [None],
        "bad",
        {},
    ],
    ids=lambda value: type(value).__name__,
)
def test_invalid_word_timing_is_rejected(words):
    service = client(
        lambda request: httpx.Response(
            200,
            content=json.dumps({"text": "hi", "words": words}),
            headers={"content-type": "application/json"},
        )
    )
    with pytest.raises(SpeechError) as error:
        service.transcribe(wav(), "audio/wav")
    assert error.value.code == "invalid_speech_response"
