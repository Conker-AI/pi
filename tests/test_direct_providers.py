"""Synthetic HTTP only: no service, key lookup, or live paid request."""

import asyncio
import json
import os
from contextlib import closing

import httpx
import pytest

from pi import model_roles
from pi.direct_providers import AnthropicProvider, OpenAIProvider, configured
from pi.loop import Loop, TurnFailed
from pi.providers import Message, ProviderUnavailable
from pi.routing import Router
from pi.store import Store


def response_for(name):
    if name == "openai":
        return {
            "model": "actual",
            "choices": [{"message": {"content": "Answer"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 5},
            },
        }
    return {
        "model": "actual",
        "content": [{"type": "text", "text": "Answer"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 4,
            "output_tokens": 3,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 5,
        },
    }


@pytest.fixture
def capture(monkeypatch):
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs))
        name = "openai" if "openai" in url else "anthropic"
        return httpx.Response(200, json=response_for(name), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    return requests


@pytest.mark.parametrize("cls", [OpenAIProvider, AnthropicProvider])
def test_text_translation_usage_and_timeout(cls, capture):
    provider = cls("synthetic", allow_paid=True)
    result = provider.complete_bounded(
        [Message("system", "Rules"), Message("user", "Q")], model="requested", timeout=1.25
    )
    url, request = capture[0]
    assert url == provider.url and request["timeout"] == 1.25
    assert request["follow_redirects"] is False
    assert request["json"]["model"] == "requested"
    assert (result.text, result.model, result.provider) == ("Answer", "actual", provider.name)
    assert (result.input_tokens, result.output_tokens, result.cached_tokens) == (12, 3, 5)
    assert result.cost_usd is None and "Answer" not in str(result.raw)
    if provider.name == "anthropic":
        assert request["json"]["system"] == [{"type": "text", "text": "Rules"}]
        assert request["json"]["messages"] == [{"role": "user", "content": "Q"}]
        assert request["json"]["max_tokens"] == 4096
        assert request["headers"]["x-api-key"] == "synthetic"
    else:
        assert request["json"]["store"] is False
        assert request["headers"]["Authorization"] == "Bearer synthetic"


@pytest.mark.parametrize("cls", [OpenAIProvider, AnthropicProvider])
@pytest.mark.parametrize("key,paid", [("", True), ("synthetic", False)])
def test_missing_authority_never_sends(cls, key, paid, capture):
    provider = cls(key, allow_paid=paid)
    assert provider.health()["status"] == "not_configured"
    with pytest.raises(ProviderUnavailable):
        provider.complete([Message("user", "Q")], model="m")
    assert not capture


@pytest.mark.parametrize("cls", [OpenAIProvider, AnthropicProvider])
@pytest.mark.parametrize(
    "status,body",
    [
        (401, {"secret": "sensitive"}),
        (429, {}),
        (302, {}),
        (200, []),
        (200, {}),
        (200, {"content": [{"type": "tool_use"}]}),
    ],
)
def test_bad_http_and_shapes_fail_without_leaking(cls, status, body, monkeypatch):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(status, json=body, request=httpx.Request("POST", url)),
    )
    with pytest.raises(ProviderUnavailable) as error:
        cls("sensitive", allow_paid=True).complete([Message("user", "sensitive")], model="m")
    assert "sensitive" not in str(error.value)
    assert error.value.__suppress_context__


def test_anthropic_rejects_moved_system_instruction(capture):
    with pytest.raises(ProviderUnavailable):
        AnthropicProvider("synthetic", allow_paid=True).complete(
            [Message("user", "Q"), Message("system", "late")], model="m"
        )
    assert not capture


def test_factory_is_explicit_and_health_does_not_call(capture):
    assert configured({}) == {}
    adapters = configured(
        {"PI_OPENAI_KEY": "synthetic", "PI_ANTHROPIC_KEY": "synthetic", "PI_ALLOW_PAID_MODELS": "1"}
    )
    assert set(adapters) == {"openai", "anthropic"}
    assert all(p.health()["status"] == "unverified" for p in adapters.values())
    assert not capture


def test_startup_registers_adapters_without_network(tmp_path, monkeypatch):
    from pi import api

    for name in list(os.environ):
        if name.startswith("PI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("PI_ADMIN_KEY", "synthetic-admin-key-only")
    monkeypatch.setenv("PI_DB_PATH", str(tmp_path / "startup.db"))
    monkeypatch.setenv("PI_OPENAI_KEY", "synthetic")
    monkeypatch.setenv("PI_ANTHROPIC_KEY", "synthetic")
    monkeypatch.setenv("PI_ALLOW_PAID_MODELS", "1")

    def no_network(*args, **kwargs):
        pytest.fail("Startup must not contact external services")

    monkeypatch.setattr(httpx, "post", no_network)
    monkeypatch.setattr(httpx, "get", no_network)

    async def check():
        async with api.lifespan(api.app):
            assert set(api.app.state.router.adapters()) == {"ollama", "openai", "anthropic"}
            monkeypatch.setattr(api.app.state.local, "health", lambda: {"status": "ok"})
            status = api.models()["direct"]
            assert set(status) == {"openai", "anthropic"}
            assert all(s["health"]["status"] == "unverified" for s in status.values())
            assert "synthetic" not in json.dumps(status)

    asyncio.run(check())


@pytest.mark.parametrize("cls", [OpenAIProvider, AnthropicProvider])
def test_timeout_is_sanitized_and_not_retried(cls, monkeypatch):
    requests = []

    def fail(*args, **kwargs):
        requests.append(kwargs)
        raise httpx.ReadTimeout("sensitive provider body")

    monkeypatch.setattr(httpx, "post", fail)
    with pytest.raises(ProviderUnavailable, match=r"^ReadTimeout$"):
        cls("synthetic", allow_paid=True).complete([Message("user", "Q")], model="m")
    assert len(requests) == 1


@pytest.mark.parametrize("provider_id", ["openai", "anthropic"])
def test_registered_direct_adapter_runs_frozen_manual_role(tmp_path, capture, provider_id):
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        disabled = {
            "enabled": False,
            "eligibleModelIds": [],
            "modelId": None,
            "timeoutMs": 1250,
            "failure": "stop",
            "fallbackModelId": None,
        }
        roles = {r: dict(disabled) for r in model_roles.ROLES}
        roles["answer"] = {**disabled, "enabled": True, "eligibleModelIds": ["m"], "modelId": "m"}
        config = model_roles.Configuration.model_validate(
            {
                "providers": [{"id": provider_id, "name": provider_id, "enabled": True}],
                "models": [
                    {
                        "id": "m",
                        "providerId": provider_id,
                        "name": "M",
                        "route": "requested",
                        "enabled": True,
                    }
                ],
                "defaultModelId": "m",
                "roleSettings": {"answerMode": "manual", "roles": roles},
            }
        )
        model_roles.save(store, model_roles.Update(expected_revision=0, configuration=config))
        adapters = configured(
            {
                "PI_OPENAI_KEY": "synthetic",
                "PI_ANTHROPIC_KEY": "synthetic",
                "PI_ALLOW_PAID_MODELS": "1",
            }
        )
        loop = Loop(store, Router(providers=adapters))
        result = loop.run_turn(sid, "Question")
        assert result["message"]["content"] == "Answer"
        assert len(capture) == 1 and capture[0][0] == adapters[provider_id].url
        assert capture[0][1]["timeout"] == 1.25
        evidence = json.loads(store.get_turn(result["turn_id"])["detail"])
        assert evidence["attempts"][-1]["actualModel"] == "actual"
        adapters[provider_id].allow_paid = False
        with pytest.raises(TurnFailed, match="unapproved substitute"):
            loop.run_turn(sid, "Second question")
        assert len(capture) == 1
