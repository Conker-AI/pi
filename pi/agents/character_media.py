"""Embedded character media checks."""

from __future__ import annotations

import base64
import binascii
import re
import zlib


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
