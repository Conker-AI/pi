from __future__ import annotations

from pathlib import Path

import pytest

from pi.provider_secrets import load


def secret(tmp_path: Path, name: str, value: bytes) -> Path:
    path = (tmp_path / name).resolve()
    path.write_bytes(value)
    return path


def test_mounted_provider_secrets_are_resolved_into_private_startup_copy(tmp_path: Path):
    openai = secret(tmp_path, "openai", b"synthetic-openai-key\n")
    anthropic = secret(tmp_path, "anthropic", b"")
    source = {
        "PI_OPENAI_KEY_FILE": str(openai),
        "PI_ANTHROPIC_KEY_FILE": str(anthropic),
        "PI_ALLOW_PAID_MODELS": "false",
    }

    resolved = load(source)

    assert resolved["PI_OPENAI_KEY"] == "synthetic-openai-key"
    assert resolved["PI_ANTHROPIC_KEY"] == ""
    assert resolved["PI_ALLOW_PAID_MODELS"] == "false"
    assert "PI_OPENAI_KEY" not in source


def test_mounted_speech_secret_uses_the_same_startup_boundary(tmp_path: Path):
    speech = secret(tmp_path, "speech", b"synthetic-speech-key\n")

    resolved = load({"PI_SPEECH_KEY_FILE": str(speech)})

    assert resolved["PI_SPEECH_KEY"] == "synthetic-speech-key"


def test_legacy_environment_and_file_cannot_both_supply_a_key(tmp_path: Path):
    path = secret(tmp_path, "openai", b"file-key-value")
    with pytest.raises(ValueError, match="not both"):
        load({"PI_OPENAI_KEY": "environment-key", "PI_OPENAI_KEY_FILE": str(path)})

    with pytest.raises(ValueError, match="not both"):
        load({"PI_SPEECH_KEY": "environment-key", "PI_SPEECH_KEY_FILE": str(path)})


@pytest.mark.parametrize("value", [b"contains space", b"line\nbreak", b"\xff" * 20, b"x" * 4097])
def test_invalid_mounted_secret_fails_closed(tmp_path: Path, value: bytes):
    path = secret(tmp_path, "openrouter", value)
    with pytest.raises(ValueError):
        load({"PI_OPENROUTER_KEY_FILE": str(path)})


def test_relative_missing_and_symlink_paths_fail_closed(tmp_path: Path):
    with pytest.raises(ValueError, match="absolute regular file"):
        load({"PI_OPENAI_KEY_FILE": "relative.key"})
    with pytest.raises(ValueError, match="absolute regular file"):
        load({"PI_OPENAI_KEY_FILE": str((tmp_path / "missing").resolve())})
    target = secret(tmp_path, "target", b"synthetic-provider-key")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("host does not permit symlink creation")
    with pytest.raises(ValueError, match="absolute regular file"):
        load({"PI_OPENAI_KEY_FILE": str(link.absolute())})
