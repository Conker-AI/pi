import json
import shutil
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from pi import audio_decode
from pi.speech import SpeechClient, SpeechError, validate_wav


@pytest.mark.parametrize(
    "media,format,codec",
    [
        ("audio/webm;codecs=opus", "webm", "libopus"),
        ("audio/ogg", "ogg", "libopus"),
        ("audio/mpeg", "mp3", "libmp3lame"),
    ],
)
def test_real_decoder_and_speech_request(media, format, codec):
    executable = shutil.which("ffmpeg")
    if not executable:
        pytest.skip("FFmpeg is not installed")
    raw = subprocess.run(
        [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.25",
            "-c:a",
            codec,
            "-f",
            format,
            "pipe:1",
        ],
        capture_output=True,
        timeout=10,
        check=True,
    ).stdout
    pcm = audio_decode.decode(raw, media, executable)
    assert 0.2 <= validate_wav(pcm, "audio/wav")["duration_seconds"] <= 0.4

    def handle(request):
        body = request.read()
        assert b"RIFF" in body and b"audio/wav" in body
        return httpx.Response(200, json={"text": "synthetic"})

    client = SpeechClient(
        "http://speech.test/v1",
        stt_model="test",
        audio_decoder=executable,
        transport=httpx.MockTransport(handle),
    )
    assert client.transcribe(raw, media)["text"] == "synthetic"
    with pytest.raises(audio_decode.DecodeError):
        audio_decode.decode(b"not an audio file", media, executable)


def test_decoder_boundary_and_sanitized_environment(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg.exe"
    executable.touch()
    monkeypatch.setenv("PRIVATE_KEY", "private-value")

    def run(argv, **kwargs):
        assert argv[argv.index("-protocol_whitelist") + 1] == "pipe"
        assert argv[argv.index("-i") + 1] == "pipe:0"
        assert "PRIVATE_KEY" not in kwargs["env"]
        assert kwargs["timeout"] == 10
        return SimpleNamespace(returncode=0, stdout=b"\x00\x00" * 16000)

    monkeypatch.setattr(audio_decode.subprocess, "run", run)
    assert (
        validate_wav(audio_decode.decode(b"fake", "audio/webm", str(executable)), "audio/wav")[
            "duration_seconds"
        ]
        == 1
    )
    monkeypatch.setattr(
        audio_decode.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"x" * (120 * 32000 + 2)),
    )
    with pytest.raises(audio_decode.DecodeError):
        audio_decode.decode(b"fake", "audio/webm", str(executable))


def test_unconfigured_decoder_and_timeout_never_contact_speech(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg.exe"
    executable.touch()

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("private details", 10)

    monkeypatch.setattr(audio_decode.subprocess, "run", timeout)
    client = SpeechClient(
        "http://speech.test/v1",
        stt_model="test",
        audio_decoder=str(executable),
        transport=httpx.MockTransport(lambda r: pytest.fail("No request expected")),
    )
    with pytest.raises(SpeechError) as error:
        client.transcribe(b"fake", "audio/webm")
    assert "private" not in json.dumps(error.value.detail)
    assert SpeechClient().capabilities()["input_mime_types"] == ["audio/wav"]
