"""Bounded English speech transport to an explicitly configured compatible server."""

import json
import math
import struct
import time
from urllib.parse import urlsplit

import httpx

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 120
MAX_TEXT_CHARACTERS = 4000
MAX_TRANSCRIPT_CHARACTERS = 16000
MAX_TRANSCRIPT_BYTES = 256 * 1024
WAV_TYPES = {"audio/wav", "audio/wave", "audio/x-wav", "audio/vnd.wave"}


class SpeechError(RuntimeError):
    def __init__(self, code, status, message):
        super().__init__(message)
        self.code, self.status = code, status
        self.detail = {"code": code, "message": message}


def _fail(code, status, message):
    raise SpeechError(code, status, message)


def validate_wav(audio, mime):
    """Validate uncompressed little-endian PCM and measure its actual frame duration."""
    media = mime.split(";", 1)[0].strip().lower() if isinstance(mime, str) else ""
    if media not in WAV_TYPES:
        _fail("unsupported_audio", 415, "Use uncompressed PCM WAV audio.")
    if not isinstance(audio, bytes) or not audio:
        _fail("invalid_audio", 422, "Provide nonempty audio bytes.")
    if len(audio) > MAX_AUDIO_BYTES:
        _fail("audio_too_large", 413, "Audio exceeds the 10 MiB limit.")
    if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        _fail("invalid_audio", 422, "Audio is not a valid PCM WAV container.")
    if struct.unpack_from("<I", audio, 4)[0] + 8 != len(audio):
        _fail("invalid_audio", 422, "WAV container length does not match the audio.")
    position, format_fields, data_size = 12, None, None
    while position < len(audio):
        if position + 8 > len(audio):
            _fail("invalid_audio", 422, "WAV chunk is truncated.")
        name = audio[position : position + 4]
        size = struct.unpack_from("<I", audio, position + 4)[0]
        start = position + 8
        if start + size > len(audio):
            _fail("invalid_audio", 422, "WAV chunk exceeds the audio boundary.")
        if name == b"fmt ":
            if format_fields is not None or size < 16:
                _fail("invalid_audio", 422, "WAV format chunk is invalid.")
            format_fields = struct.unpack_from("<HHIIHH", audio, start)
        if name == b"data":
            if data_size is not None or format_fields is None:
                _fail("invalid_audio", 422, "WAV data must follow one format chunk.")
            data_size = size
        position = start + size + (size % 2)
    if position != len(audio) or format_fields is None or not data_size:
        _fail("invalid_audio", 422, "WAV audio has missing or empty sample data.")
    encoding, channels, rate, byte_rate, alignment, bits = format_fields
    if encoding != 1 or channels not in (1, 2) or bits not in (8, 16, 24, 32):
        _fail("unsupported_audio", 415, "Use mono or stereo integer PCM WAV audio.")
    if not 8000 <= rate <= 192000 or alignment != channels * (bits // 8):
        _fail("invalid_audio", 422, "WAV sample format is invalid.")
    if byte_rate != rate * alignment or data_size % alignment:
        _fail("invalid_audio", 422, "WAV sample data is inconsistent with its format.")
    duration = (data_size // alignment) / rate
    if duration > MAX_AUDIO_SECONDS:
        _fail("audio_too_long", 413, "Audio exceeds the 120 second limit.")
    return {"mime": "audio/wav", "duration_seconds": duration}


def _label(value):
    valid = (
        isinstance(value, str)
        and 1 <= len(value) <= 200
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
        and bool(value.strip())
    )
    if valid:
        try:
            value.encode("utf-8")
        except UnicodeError:
            return False
    return valid


class SpeechClient:
    def __init__(
        self,
        url="",
        key="",
        stt_model="",
        tts_model="",
        voice="",
        *,
        timeout=30,
        transport=None,
        character_voice="unsupported",
    ):
        if character_voice not in ("unsupported", "qwen3-design"):
            _fail("invalid_configuration", 503, "Unsupported character speech adapter.")
        if not isinstance(url, str) or len(url) > 2048:
            _fail("invalid_configuration", 503, "Speech configuration is invalid.")
        if url:
            try:
                url.encode("utf-8")
                parsed = urlsplit(url)
                valid = (
                    parsed.scheme in ("http", "https")
                    and parsed.hostname
                    and not parsed.username
                    and not parsed.password
                    and not parsed.query
                    and not parsed.fragment
                    and parsed.port != 0
                    and not any(ord(c) <= 32 or ord(c) == 127 for c in url)
                )
            except ValueError:
                valid = False
            if not valid:
                _fail("invalid_configuration", 503, "Use an explicit speech API base URL.")
        if (
            not isinstance(key, str)
            or len(key) > 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in key)
        ):
            _fail("invalid_configuration", 503, "Speech credential configuration is invalid.")
        if any(
            not isinstance(value, str) or (value and not _label(value))
            for value in (stt_model, tts_model, voice)
        ):
            _fail("invalid_configuration", 503, "Speech model or voice configuration is invalid.")
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0.1 <= timeout <= 120
        ):
            _fail(
                "invalid_configuration", 503, "Speech timeout must be between 0.1 and 120 seconds."
            )
        self._url, self._key = url.rstrip("/"), key
        self._stt_model, self._tts_model, self._voice = stt_model, tts_model, voice
        self._timeout, self._transport = timeout, transport
        self._character_voice = character_voice
        self._state = {
            "stt": "configured" if url and stt_model else "unconfigured",
            "tts": "configured"
            if url and tts_model and (voice or character_voice == "qwen3-design")
            else "unconfigured",
        }

    def capabilities(self):
        """No network probe: availability describes the latest operation, not a live guarantee."""
        return {
            "stt": {"status": self._state["stt"]},
            "tts": {"status": self._state["tts"]},
            "language": "en",
            "input_mime_types": ["audio/wav"],
            "output_mime_types": ["audio/wav"],
            "max_audio_bytes": MAX_AUDIO_BYTES,
            "max_audio_seconds": MAX_AUDIO_SECONDS,
            "max_text_characters": MAX_TEXT_CHARACTERS,
            "segment_timestamps": "only_when_returned",
            "word_timestamps": "only_when_returned",
            "emotion_control": False,
            "character_voice": {
                "design": "instructions"
                if self._character_voice == "qwen3-design"
                else "unsupported",
                "reference": "unsupported",
                "delivery": "authored-instructions"
                if self._character_voice == "qwen3-design"
                else "unsupported",
                "expressiveness": "instruction-only; not a calibrated control"
                if self._character_voice == "qwen3-design"
                else "unsupported",
                "identity_consistency": "model-dependent; not guaranteed",
            },
            "streaming": False,
            "availability_basis": "configuration_and_last_operation",
        }

    def _configured(self, operation):
        if self._state[operation] == "unconfigured":
            _fail("speech_unconfigured", 503, "Configure the requested speech capability first.")

    def _request(self, operation, path, limit, **kwargs):
        started = time.monotonic()
        headers = {"Accept-Encoding": "identity"}
        if self._key:
            headers["Authorization"] = "Bearer " + self._key
        try:
            with (
                httpx.Client(
                    timeout=self._timeout,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client,
                client.stream("POST", self._url + path, headers=headers, **kwargs) as response,
            ):
                if not 200 <= response.status_code < 300:
                    _fail("speech_unavailable", 502, "Speech server rejected the request.")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    _fail(
                        "invalid_speech_response",
                        502,
                        "Compressed speech responses are unsupported.",
                    )
                length = response.headers.get("content-length")
                if length is not None and (
                    len(length) > 12 or not length.isdecimal() or int(length) > limit
                ):
                    _fail(
                        "speech_response_too_large",
                        502,
                        "Speech response exceeds its boundary.",
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > self._timeout:
                        _fail("speech_timeout", 504, "Speech request exceeded its time budget.")
                    if len(body) + len(chunk) > limit:
                        _fail(
                            "speech_response_too_large",
                            502,
                            "Speech response exceeds its boundary.",
                        )
                    body.extend(chunk)
                if length is not None and int(length) != len(body):
                    _fail(
                        "invalid_speech_response",
                        502,
                        "Speech response length is inconsistent.",
                    )
                return bytes(body), response.headers.get("content-type", "").split(";", 1)[
                    0
                ].lower()
        except SpeechError:
            self._state[operation] = "unavailable"
            raise
        except httpx.TimeoutException:
            self._state[operation] = "unavailable"
            raise SpeechError("speech_timeout", 504, "Speech server timed out.") from None
        except httpx.HTTPError:
            self._state[operation] = "unavailable"
            raise SpeechError("speech_unavailable", 502, "Speech server is unavailable.") from None

    def transcribe(self, audio: bytes, mime: str):
        self._configured("stt")
        measured = validate_wav(audio, mime)
        body, media = self._request(
            "stt",
            "/audio/transcriptions",
            MAX_TRANSCRIPT_BYTES,
            data={
                "model": self._stt_model,
                "language": "en",
                "response_format": "verbose_json",
                "timestamp_granularities[]": ["segment", "word"],
            },
            files={"file": ("turn.wav", audio, "audio/wav")},
        )
        try:
            if media != "application/json":
                raise ValueError("Expected JSON")

            def unique(pairs):
                if len({key for key, _ in pairs}) != len(pairs):
                    raise ValueError("Duplicate response key")
                return dict(pairs)

            value = json.loads(body, object_pairs_hook=unique)
            text = value["text"]
            if not isinstance(text, str) or len(text) > MAX_TRANSCRIPT_CHARACTERS or "\x00" in text:
                raise ValueError("Invalid transcript")
            text.encode("utf-8")
            language = value.get("language")
            if language is not None and (not _label(language) or len(language) > 32):
                raise ValueError("Invalid language")
            raw_segments, segments = value.get("segments"), None
            if raw_segments is not None:
                if not isinstance(raw_segments, list) or len(raw_segments) > 1000:
                    raise ValueError("Invalid segments")
                segments, previous, characters = [], 0, 0
                for segment in raw_segments:
                    start, end, segment_text = segment["start"], segment["end"], segment["text"]
                    if any(
                        type(v) not in (int, float) or not math.isfinite(v) for v in (start, end)
                    ):
                        raise ValueError("Invalid timestamps")
                    if not previous <= start <= end <= measured["duration_seconds"]:
                        raise ValueError("Timestamps exceed audio or are not monotonic")
                    if not isinstance(segment_text, str) or "\x00" in segment_text:
                        raise ValueError("Invalid segment text")
                    segment_text.encode("utf-8")
                    characters += len(segment_text)
                    if characters > MAX_TRANSCRIPT_CHARACTERS:
                        raise ValueError("Segment text exceeds boundary")
                    segments.append({"start": start, "end": end, "text": segment_text})
                    previous = end
            words = value.get("words")
            if words is not None:
                if not isinstance(words, list) or len(words) > 4000:
                    raise ValueError("Invalid words")
                normalized, previous, characters = [], 0, 0
                for word in words:
                    start, end, token = word["start"], word["end"], word["word"]
                    if any(
                        type(v) not in (int, float) or not math.isfinite(v) for v in (start, end)
                    ):
                        raise ValueError("Invalid word timestamp")
                    if not previous <= start <= end <= measured["duration_seconds"]:
                        raise ValueError("Invalid word timing")
                    if not isinstance(token, str) or not token.strip() or "\x00" in token:
                        raise ValueError("Invalid word text")
                    token.encode("utf-8")
                    characters += len(token)
                    if characters > MAX_TRANSCRIPT_CHARACTERS:
                        raise ValueError("Word text exceeds boundary")
                    normalized.append({"start": start, "end": end, "word": token})
                    previous = end
                words = normalized
        except (ValueError, TypeError, KeyError, RecursionError):
            self._state["stt"] = "unavailable"
            raise SpeechError(
                "invalid_speech_response", 502, "Speech server returned an invalid transcript."
            ) from None
        self._state["stt"] = "available"
        return {
            "text": text,
            "segments": segments,
            "words": words,
            "language": language,
            "duration_seconds": measured["duration_seconds"],
        }

    def _presentation(self, presentation):
        """Map a frozen, media-free character snapshot to explicit server extensions."""
        if self._character_voice != "qwen3-design":
            _fail(
                "character_voice_unsupported",
                422,
                "Configured speech adapter cannot honor character voice settings.",
            )
        from .characters import Mode, Voice

        try:
            studio = presentation["character"]["profile"]["studio"]
            selected = presentation["mode"]
            if selected not in ("focus", "character"):
                raise ValueError("Invalid mode")
            # Reference audio is deliberately absent from runtime snapshots and this adapter.
            raw_voice = dict(studio["voice"])
            if raw_voice.get("source") == "reference":
                _fail(
                    "character_reference_unsupported",
                    422,
                    "Reference voice synthesis is unsupported by this adapter.",
                )
            raw_voice["reference"] = None
            voice = Voice.model_validate(raw_voice)
            delivery = Mode.model_validate(studio["modes"][selected])
        except (KeyError, TypeError, ValueError):
            raise SpeechError(
                "invalid_character_voice", 422, "Character speech settings are invalid."
            ) from None
        if voice.language != "English":
            _fail(
                "character_language_unsupported",
                422,
                "Call speech currently supports English only.",
            )
        if not voice.description.strip():
            _fail(
                "character_design_required",
                422,
                "Describe the character voice before synthesizing its design.",
            )
        instructions = (
            "Voice identity: " + voice.description + "\n"
            "Pronunciation guidance: " + voice.pronunciation + "\n"
            "Delivery mode: " + selected + "\n"
            "Delivery guidance: " + delivery.voice + "\n"
            f"Authored expressiveness preference (0 restrained, 100 expressive): "
            f"{delivery.expressiveness:g}.\n"
            "Preserve the voice identity and speak only the supplied input text."
        )
        if selected == "focus":
            instructions += " Use clear, restrained, natural delivery without character flourishes."
        if "\x00" in instructions:
            _fail("invalid_character_voice", 422, "Character speech settings contain NUL.")
        try:
            instructions.encode("utf-8")
        except UnicodeError:
            raise SpeechError(
                "invalid_character_voice", 422, "Character speech settings must be valid Unicode."
            ) from None
        return {"task_type": "VoiceDesign", "language": "English", "instructions": instructions}

    def synthesize(self, text: str, *, presentation=None):
        self._configured("tts")
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            _fail("invalid_speech_text", 422, "Provide nonblank speech text without NUL.")
        if len(text) > MAX_TEXT_CHARACTERS:
            _fail("speech_text_too_large", 413, "Speech text exceeds 4000 characters.")
        try:
            text.encode("utf-8")
        except UnicodeError:
            raise SpeechError(
                "invalid_speech_text", 422, "Speech text must be valid Unicode."
            ) from None
        payload = {
            "model": self._tts_model,
            "voice": self._voice,
            "input": text,
            "response_format": "wav",
        }
        if presentation is not None:
            payload.update(self._presentation(presentation))
            # VoiceDesign has no preset speaker; do not silently request another identity.
            payload.pop("voice")
        elif not self._voice:
            _fail("speech_unconfigured", 503, "Configure a default voice for ordinary synthesis.")
        body, media = self._request(
            "tts",
            "/audio/speech",
            MAX_AUDIO_BYTES,
            json=payload,
        )
        try:
            measured = validate_wav(body, media)
        except SpeechError:
            self._state["tts"] = "unavailable"
            raise SpeechError(
                "invalid_speech_response", 502, "Speech server returned invalid PCM WAV audio."
            ) from None
        self._state["tts"] = "available"
        return {"audio": body, **measured}
