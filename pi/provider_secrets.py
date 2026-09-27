"""Read provider credentials from fixed host-mounted files without exposing their values."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

MAX_SECRET_BYTES = 4096
PROVIDER_KEYS = (
    "PI_OPENROUTER_KEY",
    "PI_OPENAI_KEY",
    "PI_ANTHROPIC_KEY",
    "PI_SPEECH_KEY",
)


def _read(path_value: str, name: str) -> str:
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError(f"{name}_FILE must name an absolute regular file.")
    with path.open("rb") as source:
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
        character.isspace() or ord(character) < 33 or ord(character) > 126
        for character in text
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
