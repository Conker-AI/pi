import json

import httpx
import pytest

from pi import live_stream
from pi.chatgpt_provider import ChatGPTProvider, configured
from pi.providers import Message, ProviderUnavailable


def events(*rows):
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


def completion():
    return {
        "type": "done",
        "model": "test-model",
        "inputTokens": 3,
        "outputTokens": 1,
        "cachedTokens": None,
    }


def adapter(body, status=200):
    def host(request):
        assert request.url.host == "conker-chatgpt"
        assert "authorization" not in request.headers
        assert "tools" not in json.loads(request.content)
        return httpx.Response(
            status, headers={"content-type": "application/x-ndjson"}, content=body
        )

    return ChatGPTProvider("/private/inference.sock", transport=httpx.MockTransport(host))


def test_subscription_does_not_require_api_key_or_paid_api_switch():
    assert "chatgpt" in configured({"PI_CHATGPT_SOCKET": "/private/inference.sock"}, 30)
    assert not configured({}, 30)
    provider = adapter(events({"type": "text", "delta": "Hello"}, completion()))
    result = provider.complete([Message("user", "hi")], model="test-model")
    assert result.text == "Hello" and result.provider == "chatgpt"
    assert result.cost_usd is None


@pytest.mark.parametrize(
    "body",
    [
        events({"type": "text", "delta": "partial"}),
        events({"type": "text", "delta": "text"}, {**completion(), "model": "wrong"}),
        events({"type": "reasoning", "text": "private"}, completion()),
        events({"type": "text", "delta": "text"}, {**completion(), "inputTokens": True}),
    ],
)
def test_invalid_or_partial_completion_is_never_saved_as_answer(body):
    with pytest.raises(ProviderUnavailable):
        adapter(body).complete([Message("user", "hi")], model="test-model")


def test_auth_and_quota_errors_are_safe_and_actionable():
    with pytest.raises(ProviderUnavailable, match="Settings > Providers"):
        adapter(events({"type": "error", "code": "not_connected"})).complete(
            [Message("user", "hi")], model="test-model"
        )
    with pytest.raises(ProviderUnavailable, match="usage limit"):
        adapter(events({"type": "error", "code": "usage_limit"})).complete(
            [Message("user", "hi")], model="test-model"
        )


def test_preview_and_stop_use_existing_pi_turn_controls():
    with live_stream.open_reply("subscription-test"), live_stream.answering():
        provider = adapter(events({"type": "text", "delta": "Hello"}, completion()))
        provider.complete([Message("user", "hi")], model="test-model")
        assert any(
            event.get("text") == "Hello" for event in live_stream.read("subscription-test")[0]
        )
        live_stream.stop("subscription-test")
        with pytest.raises(ProviderUnavailable, match="Owner stopped"):
            provider.complete([Message("user", "hi")], model="test-model")


@pytest.mark.parametrize("connected,expected", [(True, "unverified"), (False, "not_configured")])
def test_auth_is_not_an_inference_receipt(connected, expected):
    provider = ChatGPTProvider(
        "/private/socket",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"connected": connected, "available": True})
        ),
    )
    assert provider.health()["status"] == expected
