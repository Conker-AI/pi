"""Optional local media normalization; no files, network inputs or shell commands."""

import io
import os
import subprocess
import sys
import threading
import wave
from pathlib import Path

FORMATS = {"audio/webm": "matroska", "audio/ogg": "ogg", "audio/mpeg": "mp3"}
WORKERS = threading.BoundedSemaphore(2)


class DecodeError(ValueError):
    pass


def decode(raw, mime, executable):
    media = mime.split(";", 1)[0].strip().lower() if isinstance(mime, str) else ""
    if media not in FORMATS or type(raw) is not bytes or not raw or len(raw) > 10 * 1024 * 1024:
        raise DecodeError("Unsupported or excessive compressed audio.")
    if not executable or not Path(executable).is_absolute() or not Path(executable).is_file():
        raise DecodeError("A local audio decoder must be configured.")
    if not WORKERS.acquire(timeout=1):
        raise DecodeError("Audio decoder is busy.")
    try:
        result = subprocess.run(
            [
                executable,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-xerror",
                "-max_alloc",
                "33554432",
                "-protocol_whitelist",
                "pipe",
                "-threads",
                "1",
                "-f",
                FORMATS[media],
                "-i",
                "pipe:0",
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-t",
                "121",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-threads",
                "1",
                "-f",
                "s16le",
                "pipe:1",
            ],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            env={
                key: os.environ[key]
                for key in ("SYSTEMROOT", "WINDIR", "PATH")
                if key in os.environ
            },
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            check=False,
        )
        pcm = result.stdout
        if result.returncode or not pcm or len(pcm) % 2 or len(pcm) > 120 * 32000:
            raise DecodeError("Audio is invalid or exceeds 120 seconds.")
        output = io.BytesIO()
        with wave.open(output, "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(pcm)
        return output.getvalue()
    except (OSError, subprocess.SubprocessError):
        raise DecodeError("Audio decoding failed or timed out.") from None
    finally:
        WORKERS.release()
