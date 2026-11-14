"""The Claude plan answers with the model id the runtime resolved, never with a malformed value."""

import httpx
import pytest

from pi.models.claude_code import ClaudeCodeProvider
from pi.models.providers import Message


def answer(**extra):
    provider = ClaudeCodeProvider(
        "",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"text": "ok", "model": "haiku", **extra})
        ),
    )
    return provider.complete_bounded([Message("user", "Hi")], model="haiku", timeout=5)


def test_the_resolved_model_id_is_the_answering_model():
    assert answer(actualModel="claude-haiku-4-5-20251001").model == "claude-haiku-4-5-20251001"


def test_a_missing_actual_model_keeps_the_alias_asked():
    assert answer().model == "haiku"
    assert answer(actualModel=None).model == "haiku"
    assert answer(actualModel="").model == "haiku"


@pytest.mark.parametrize(
    "bad", [{"id": "x"}, 5, ["claude-haiku"], True, "has space", "-lead", "a" * 301, "x\ny"]
)
def test_a_malformed_actual_model_never_becomes_the_answering_model(bad):
    completion = answer(actualModel=bad)
    assert completion.model == "haiku"
    assert isinstance(completion.model, str)
