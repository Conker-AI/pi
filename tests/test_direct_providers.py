"""Synthetic HTTP only: no service, key lookup, or live paid request."""

import asyncio
import json
import os
from contextlib import closing

import httpx
import pytest

from pi import inference_control, model_roles, research_usage
from pi.direct_providers import AnthropicProvider, OpenAIProvider, configured
from pi.loop import Loop, TurnFailed
from pi.providers import Message, ProviderUnavailable
from pi.routing import Router
from pi.store import Store


@pytest.fixture(autouse=True)
def reject_unmocked_network(monkeypatch):
    def reject(*_args, **_kwargs):
        pytest.fail("Direct-provider tests must use a synthetic HTTP transport")

    async def reject_async(*_args, **_kwargs):
        reject()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_async)


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


@pytest.fixture
def stream_capture(monkeypatch):
    observed = {"requests": [], "clients": [], "streams": []}
    real_client = httpx.Client

    class SSE(httpx.SyncByteStream):
        def __init__(self, body):
            self.body, self.delivered, self.closed = body, False, False

        def __iter__(self):
            yield self.body[:17]
            self.delivered = True
            yield self.body[17:]

        def close(self):
            self.closed = True

    def reply(request):
        assert inference_control.current() is not None
        assert request.method == "POST"
        assert str(request.url) in {OpenAIProvider.url, AnthropicProvider.url}
        observed["requests"].append(request)
        name = "openai" if str(request.url) == OpenAIProvider.url else "anthropic"
        result = response_for(name)
        if name == "openai":
            events = [
                {"model": "actual", "choices": [{"delta": {"content": "Answer"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": result["usage"]},
            ]
        else:
            events = [
                {"type": "message_start", "message": {"model": "actual", "usage": result["usage"]}},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Answer"},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 3},
                },
                {"type": "message_stop"},
            ]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        if name == "openai":
            body += "data: [DONE]\n\n"
        stream = SSE(body.encode())
        observed["streams"].append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        result = real_client(**kwargs, transport=httpx.MockTransport(reply))
        observed["clients"].append(result)
        return result

    monkeypatch.setattr(httpx, "Client", client)
    return observed


@pytest.mark.parametrize("cls", [OpenAIProvider, AnthropicProvider])
def test_text_translation_usage_and_timeout(cls, capture):
    provider = cls("synthetic", allow_paid=True)
    result = provider.complete_bounded(
        [Message("system", "Rules"), Message("user", "Q")], model="requested", timeout=1.25
    )
    url, request = capture[0]
    assert url == provider.url and request["timeout"] == 1.25
    assert request["follow_redirects"] is False
    assert request["trust_env"] is False
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
            assert api.app.state.provider_credentials == ["anthropic", "openai"]
            monkeypatch.setattr(api.app.state.local, "health", lambda: {"status": "ok"})
            status = api.models()["direct"]
            assert set(status) == {"openai", "anthropic"}
            assert all(s["health"]["status"] == "unverified" for s in status.values())
            assert "synthetic" not in json.dumps(status)
            api._health_cache.clear()
            health = api.health()["checks"]["provider_credentials"]
            assert health == {
                "status": "ok",
                "configured": ["anthropic", "openai"],
                "verification": "host-revision-bound",
                "secrets_included": False,
            }

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
def test_registered_direct_adapter_runs_frozen_manual_role(tmp_path, stream_capture, provider_id):
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
        requests = stream_capture["requests"]
        assert len(requests) == 1 and str(requests[0].url) == adapters[provider_id].url
        assert requests[0].extensions["timeout"] == dict(
            connect=1.25, read=1.25, write=1.25, pool=1.25
        )
        payload = json.loads(requests[0].content)
        assert payload["model"] == "requested" and payload["stream"] is True
        assert payload["messages"][-1] == {"role": "user", "content": "Question"}
        if provider_id == "openai":
            assert requests[0].headers["authorization"] == "Bearer synthetic"
            assert payload["stream_options"] == {"include_usage": True}
        else:
            assert requests[0].headers["x-api-key"] == "synthetic"
        assert len(stream_capture["clients"]) == len(stream_capture["streams"]) == 1
        assert stream_capture["clients"][0].is_closed
        assert stream_capture["streams"][0].delivered and stream_capture["streams"][0].closed
        evidence = json.loads(store.get_turn(result["turn_id"])["detail"])
        assert evidence["attempts"][-1]["actualModel"] == "actual"
        usage = research_usage.receipt(store, result["turn_id"])
        assert usage["foreground"]["attempt_count"] == 1
        assert {
            key: usage["foreground"]["totals"][key]
            for key in ("input_tokens", "output_tokens", "cached_tokens")
        } == dict(input_tokens=12, output_tokens=3, cached_tokens=5)
        assert usage["foreground"]["totals"]["cost_usd"] is None
        assert usage["attempts"][0]["actual_provider"] == provider_id
        assert usage["attempts"][0]["actual_model"] == "actual"
        adapters[provider_id].allow_paid = False
        with pytest.raises(TurnFailed, match="unapproved substitute"):
            loop.run_turn(sid, "Second question")
        assert len(requests) == len(stream_capture["clients"]) == 1
