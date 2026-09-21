"""Synthetic PCM media and HTTP transports only; no microphone or speech services."""

import io
import json
import struct
import wave

import httpx
import pytest

from pi import speech


def wav(seconds=1, rate=8000, channels=1, width=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        stream.writeframes(b"\x00" * int(seconds * rate) * channels * width)
    return output.getvalue()


def client(handler):
    return speech.SpeechClient(
        "http://speech.test/v1",
        "synthetic-key",
        "stt-model",
        "tts-model",
        "english-voice",
        transport=httpx.MockTransport(handler),
    )


def test_transcription_returns_only_actual_timing_and_english_request():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == "/v1/audio/transcriptions"
        body = request.read()
        assert b'name="language"\r\n\r\nen' in body
        assert b"verbose_json" in body and b"timestamp_granularities[]" in body
        assert b'filename="turn.wav"' in body and b"RIFF" in body
        return httpx.Response(
            200,
            json={
                "text": "Hello",
                "language": "english",
                "segments": [
                    {"start": 0.2, "end": 0.8, "text": "Hello", "untrusted_extra": "ignored"}
                ],
            },
        )

    service = client(handle)
    assert service.capabilities()["stt"]["status"] == "configured"
    result = service.transcribe(wav(), "audio/x-wav")
    assert result == {
        "text": "Hello",
        "language": "english",
        "duration_seconds": 1.0,
        "segments": [{"start": 0.2, "end": 0.8, "text": "Hello"}],
    }
    assert len(calls) == 1 and calls[0].headers["authorization"] == "Bearer synthetic-key"
    assert service.capabilities()["stt"]["status"] == "available"
    assert service.capabilities()["tts"]["status"] == "configured"


def test_absent_timing_and_silence_are_not_fabricated():
    service = client(lambda request: httpx.Response(200, json={"text": ""}))
    assert service.transcribe(wav(), "audio/wav") == {
        "text": "",
        "segments": None,
        "duration_seconds": 1.0,
        "language": None,
    }
    assert service.capabilities()["word_timestamps"] is False
    assert service.capabilities()["emotion_control"] is False


def test_synthesis_requests_explicit_voice_and_returns_measured_audio():
    audio = wav(0.5, channels=2)

    def handle(request):
        assert request.url.path == "/v1/audio/speech"
        assert json.loads(request.read()) == {
            "model": "tts-model",
            "voice": "english-voice",
            "input": "Hello",
            "response_format": "wav",
        }
        return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

    service = client(handle)
    result = service.synthesize("Hello")
    assert result == {"audio": audio, "mime": "audio/wav", "duration_seconds": 0.5}
    assert service.capabilities()["tts"]["status"] == "available"


def test_unconfigured_does_not_attempt_network():
    def forbidden(request):
        pytest.fail("Unconfigured client must not connect")

    service = speech.SpeechClient(transport=httpx.MockTransport(forbidden))
    assert service.capabilities()["stt"]["status"] == "unconfigured"
    with pytest.raises(speech.SpeechError, match="Configure"):
        service.transcribe(wav(), "audio/wav")
    with pytest.raises(speech.SpeechError, match="Configure"):
        service.synthesize("Hello")


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://user:secret@speech.test/v1"},
        {"url": "https://speech.test/v1?key=secret"},
        {"url": "file:///speech"},
        {"url": "http://speech.test:bad"},
        {"key": "secret\r\ninjected"},
        {"timeout": float("inf")},
        {"timeout": True},
        {"stt_model": 0},
    ],
    ids=lambda value: type(value).__name__,
)
def test_invalid_configuration_does_not_echo_secrets(changes):
    with pytest.raises(speech.SpeechError) as failure:
        speech.SpeechClient(**changes)
    assert failure.value.code == "invalid_configuration"
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize(
    "audio,mime,code",
    [
        (b"", "audio/wav", "invalid_audio"),
        (b"not a WAV", "audio/wav", "invalid_audio"),
        (b"anything", "audio/webm", "unsupported_audio"),
        (wav()[:-1], "audio/wav", "invalid_audio"),
        (wav() + b"trailing", "audio/wav", "invalid_audio"),
        (wav(121), "audio/wav", "audio_too_long"),
        (b"x" * (speech.MAX_AUDIO_BYTES + 1), "audio/wav", "audio_too_large"),
    ],
    ids=lambda value: type(value).__name__,
)
def test_bad_audio_is_rejected_before_transport(audio, mime, code):
    def forbidden(request):
        pytest.fail("Invalid audio must not connect")

    service = client(forbidden)
    with pytest.raises(speech.SpeechError) as failure:
        service.transcribe(audio, mime)
    assert failure.value.code == code
    assert service.capabilities()["stt"]["status"] == "configured"


def test_wav_frame_arithmetic_and_format_are_validated():
    for position, format, value in [
        (20, "<H", 3),
        (22, "<H", 9),
        (24, "<I", 0),
        (28, "<I", 1),
        (32, "<H", 1),
    ]:
        malformed = bytearray(wav())
        struct.pack_into(format, malformed, position, value)
        with pytest.raises(speech.SpeechError):
            speech.validate_wav(bytes(malformed), "audio/wav")


@pytest.mark.parametrize(
    "segments",
    [
        [{"start": 0, "end": 1.1, "text": "Hello"}],
        [{"start": -1, "end": 0.5, "text": "Hello"}],
        [{"start": 0.5, "end": 0.9, "text": "One"}, {"start": 0.8, "end": 1, "text": "Two"}],
        [{"start": True, "end": 1, "text": "Hello"}],
        [{"start": 0, "end": float("nan"), "text": "Hello"}],
        [{"start": 0, "end": 1, "text": 123}],
        "invented",
    ],
    ids=lambda value: type(value).__name__,
)
def test_invalid_segment_boundaries_fail_without_inventing_timing(segments):
    service = client(
        lambda r: httpx.Response(
            200,
            content=json.dumps({"text": "Hello", "segments": segments}),
            headers={"content-type": "application/json"},
        )
    )
    with pytest.raises(speech.SpeechError) as failure:
        service.transcribe(wav(), "audio/wav")
    assert failure.value.code == "invalid_speech_response"
    assert service.capabilities()["stt"]["status"] == "unavailable"


@pytest.mark.parametrize(
    "body,media",
    [(b"<html>secret</html>", "audio/wav"), (wav(), "audio/mpeg"), (wav()[:-2], "audio/wav")],
    ids=lambda value: type(value).__name__,
)
def test_synthesis_does_not_trust_media_label_or_relabel_other_formats(body, media):
    service = client(lambda r: httpx.Response(200, content=body, headers={"content-type": media}))
    with pytest.raises(speech.SpeechError) as failure:
        service.synthesize("Hello")
    assert failure.value.code == "invalid_speech_response"
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize(
    "status", [301, 302, 307, 401, 429, 500], ids=lambda value: type(value).__name__
)
def test_http_errors_and_redirects_never_retry_or_disclose_body(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status, content=b"secret error", headers={"location": "https://elsewhere.test/"}
        )

    service = client(handle)
    with pytest.raises(speech.SpeechError) as failure:
        service.synthesize("Hello")
    assert len(calls) == 1 and failure.value.status == 502
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize(
    "error,code",
    [(httpx.ReadTimeout, "speech_timeout"), (httpx.ConnectError, "speech_unavailable")],
    ids=lambda value: type(value).__name__,
)
def test_transport_errors_are_static(error, code):
    calls = []

    def handle(request):
        calls.append(request)
        assert all(v == 30 for v in request.extensions["timeout"].values())
        raise error("secret credential and request body", request=request)

    with pytest.raises(speech.SpeechError) as failure:
        client(handle).synthesize("Hello")
    assert len(calls) == 1 and failure.value.code == code
    assert "secret" not in str(failure.value)


class BytesStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


def test_stream_without_length_is_bounded():
    service = client(
        lambda r: httpx.Response(
            200,
            stream=BytesStream([b"x" * speech.MAX_TRANSCRIPT_BYTES, b"x"]),
            headers={"content-type": "application/json"},
        )
    )
    with pytest.raises(speech.SpeechError) as failure:
        service.transcribe(wav(), "audio/wav")
    assert failure.value.code == "speech_response_too_large"


def test_slow_drip_hits_total_deadline(monkeypatch):
    ticks = iter([0, 10, 31])
    monkeypatch.setattr(speech.time, "monotonic", lambda: next(ticks))
    service = client(
        lambda r: httpx.Response(
            200, stream=BytesStream([b"a", b"b"]), headers={"content-type": "audio/wav"}
        )
    )
    with pytest.raises(speech.SpeechError) as failure:
        service.synthesize("Hello")
    assert failure.value.code == "speech_timeout"


@pytest.mark.parametrize(
    "text", ["", " ", "a\x00b", "x" * 4001], ids=lambda value: type(value).__name__
)
def test_tts_text_bound_is_checked_before_transport(text):
    service = client(lambda r: pytest.fail("Invalid text must not connect"))
    with pytest.raises(speech.SpeechError):
        service.synthesize(text)
