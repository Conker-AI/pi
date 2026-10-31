"""Read provider credentials from fixed host-mounted files without exposing their values."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

MAX_SECRET_BYTES = 4096
# The open refuses a final symlink where the OS can, and never waits on a pipe.
OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
PROVIDER_KEYS = (
    "PI_OPENROUTER_KEY",
    "PI_OPENAI_KEY",
    "PI_ANTHROPIC_KEY",
    "PI_GEMINI_KEY",
    "PI_XAI_KEY",
    "PI_MISTRAL_KEY",
    "PI_DEEPSEEK_KEY",
    "PI_GROQ_KEY",
    "PI_SPEECH_KEY",
)


def _not_regular(name: str) -> ValueError:
    return ValueError(f"{name}_FILE must name an absolute regular file.")


def _read(path_value: str, name: str) -> str:
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise _not_regular(name)
    # The path can change between those checks and the open, so what was opened is checked again.
    try:
        descriptor = os.open(path, OPEN_FLAGS)
    except OSError as exc:
        raise _not_regular(name) from exc
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise _not_regular(name)
        value = source.read(MAX_SECRET_BYTES + 2)
    if value.endswith(b"\n"):
        value = value[:-1]
    if value.endswith(b"\r"):
        value = value[:-1]
    if len(value) > MAX_SECRET_BYTES:
        raise ValueError(f"{name}_FILE exceeds 4096 bytes.")
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name}_FILE must contain ASCII.") from exc
    if text and any(
        character.isspace() or ord(character) < 33 or ord(character) > 126 for character in text
    ):
        raise ValueError(f"{name}_FILE must contain one printable token without spaces.")
    return text


def load(environment: Mapping[str, str]) -> dict[str, str]:
    """Return a private startup copy with mounted provider values resolved once."""
    resolved = dict(environment)
    for name in PROVIDER_KEYS:
        direct = environment.get(name, "").strip()
        path = environment.get(name + "_FILE", "").strip()
        if direct and path:
            raise ValueError(f"Configure {name} or {name}_FILE, not both.")
        if path:
            resolved[name] = _read(path, name)
    return resolved
