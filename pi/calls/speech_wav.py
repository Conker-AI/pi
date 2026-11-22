"""WAV validation and the speech error type."""

import struct
from typing import NoReturn

from pi.core.errors import ServiceError

MAX_AUDIO_BYTES = 10 * 1024 * 1024


MAX_AUDIO_SECONDS = 120


WAV_TYPES = {"audio/wav", "audio/wave", "audio/x-wav", "audio/vnd.wave"}


class SpeechError(ServiceError, RuntimeError):
    def __init__(self, code, status, message):
        super().__init__(code, message, status)


def _fail(code, status, message) -> NoReturn:
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
