"""Direct OpenAI-compatible vendors (Gemini, xAI, Mistral, DeepSeek, Groq). Synthetic HTTP only."""

import asyncio
import json
import os
from contextlib import closing

import httpx
import pytest

from pi.api import startup
from pi.conversation import output_settings
from pi.conversation.store import Store
from pi.models import roles as model_roles
from pi.models import secrets
from pi.models.direct import (
    ADAPTERS,
    DeepSeekProvider,
    GeminiProvider,
    GroqProvider,
    MistralProvider,
    XAIProvider,
    configured,
)
from pi.models.providers import ImageInput, Message, ProviderUnavailable
from pi.models.routing import Router
from pi.turns import inference_control
from pi.turns.loop import Loop

# Endpoints from each vendor's documentation (see docs/direct-providers.md).
VENDORS = {
    "gemini": (
        "PI_GEMINI_KEY",
        GeminiProvider,
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    ),
    "xai": ("PI_XAI_KEY", XAIProvider, "https://api.x.ai/v1/chat/completions"),
    "mistral": ("PI_MISTRAL_KEY", MistralProvider, "https://api.mistral.ai/v1/chat/completions"),
    "deepseek": ("PI_DEEPSEEK_KEY", DeepSeekProvider, "https://api.deepseek.com/chat/completions"),
    "groq": ("PI_GROQ_KEY", GroqProvider, "https://api.groq.com/openai/v1/chat/completions"),
}
IDS = list(VENDORS)
KEY = "synthetic-vendor-key"
BODY = {
    "model": "actual",
    "choices": [{"message": {"content": "Answer"}, "finish_reason": "stop"}],
    "usage": {
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "prompt_tokens_details": {"cached_tokens": 5},
    },
}


@pytest.fixture(autouse=True)
def reject_unmocked_network(monkeypatch):
    def reject(*_args, **_kwargs):
        pytest.fail("Direct-provider tests must use a synthetic HTTP transport")

    async def reject_async(*_args, **_kwargs):
        reject()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_async)


@pytest.fixture
def capture(monkeypatch):
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(200, json=BODY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    return requests


@pytest.fixture
def stream_capture(monkeypatch):
    observed = {"requests": [], "clients": []}
    real_client = httpx.Client

    def reply(request):
        assert inference_control.current() is not None
        observed["requests"].append(request)
        events = [
            {"model": "actual", "choices": [{"delta": {"content": "Ans"}}]},
            {"choices": [{"delta": {"content": "wer"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": BODY["usage"]},
        ]
        body = "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        result = real_client(**kwargs, transport=httpx.MockTransport(reply))
        observed["clients"].append(result)
        return result

    monkeypatch.setattr(httpx, "Client", client)
    return observed


def test_every_vendor_is_registered_with_its_documented_endpoint():
    registered = {adapter.name: (key, adapter, adapter.url) for key, adapter in ADAPTERS}
    assert {name: registered[name] for name in IDS} == VENDORS
    assert set(registered) == {"openai", "anthropic", *IDS}
    for key, _cls, _url in VENDORS.values():
        assert key in secrets.PROVIDER_KEYS
    assert set(IDS) <= output_settings.HOSTED_PROVIDERS


@pytest.mark.parametrize("provider_id", IDS)
def test_request_url_headers_payload_and_usage(provider_id, capture):
    _key, cls, url = VENDORS[provider_id]
    image = ImageInput("image/png", "aGk=")
    result = cls(KEY, allow_paid=True).complete_bounded(
        [Message("system", "Rules"), Message("user", "Q", images=(image,))],
        model="requested",
        timeout=1.25,
    )
    sent_url, request = capture[0]
    assert sent_url == url and request["timeout"] == 1.25
    assert request["follow_redirects"] is False and request["trust_env"] is False
    assert request["headers"] == {"Authorization": f"Bearer {KEY}"}
    # No vendor but OpenAI documents `store`; the body is plain Chat Completions.
    assert request["json"] == {
        "model": "requested",
        "messages": [
            {"role": "system", "content": "Rules"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Q"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="}},
                ],
            },
        ],
    }
    assert (result.text, result.model, result.provider) == ("Answer", "actual", provider_id)
    assert (result.input_tokens, result.output_tokens, result.cached_tokens) == (12, 3, 5)
    assert result.cost_usd is None


@pytest.mark.parametrize("provider_id", IDS)
@pytest.mark.parametrize("key,paid", [("", True), (KEY, False)])
def test_missing_key_or_paid_opt_in_never_sends(provider_id, key, paid, capture):
    provider = VENDORS[provider_id][1](key, allow_paid=paid)
    assert provider.health()["status"] == "not_configured"
    with pytest.raises(ProviderUnavailable):
        provider.complete([Message("user", "Q")], model="m")
    assert not capture


@pytest.mark.parametrize("provider_id", IDS)
def test_missing_key_is_not_configured(provider_id):
    env_name = VENDORS[provider_id][0]
    assert provider_id not in configured({env_name: "  ", "PI_ALLOW_PAID_MODELS": "1"})
    assert provider_id in configured({env_name: KEY})


@pytest.mark.parametrize("provider_id", IDS)
@pytest.mark.parametrize("status", [400, 401, 403, 422, 429, 302])
def test_rejected_key_is_not_echoed(provider_id, status, monkeypatch):
    # Vendors echo the rejected key or the prompt in error bodies; none of it may cross the adapter.
    body = {"error": {"message": f"Incorrect API key provided: {KEY}", "prompt": "private"}}
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(status, json=body, request=httpx.Request("POST", url)),
    )
    provider = VENDORS[provider_id][1](KEY, allow_paid=True)
    with pytest.raises(ProviderUnavailable) as error:
        provider.complete([Message("user", "private")], model="m")
    assert KEY not in str(error.value) and "private" not in str(error.value)
    assert error.value.__suppress_context__
    assert KEY not in json.dumps(provider.health())


@pytest.mark.parametrize("provider_id", IDS)
def test_streaming_turn_uses_vendor_endpoint(tmp_path, stream_capture, provider_id):
    env_name, _cls, url = VENDORS[provider_id]
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
        adapters = configured({env_name: KEY, "PI_ALLOW_PAID_MODELS": "1"})
        result = Loop(store, Router(providers=adapters)).run_turn(sid, "Question")
        assert result["message"]["content"] == "Answer"
        (request,) = stream_capture["requests"]
        assert str(request.url) == url
        assert request.headers["authorization"] == f"Bearer {KEY}"
        payload = json.loads(request.content)
        assert payload["model"] == "requested" and payload["stream"] is True
        assert "store" not in payload
        if provider_id == "mistral":
            # Mistral's chat schema has no stream_options.
            assert "stream_options" not in payload
        else:
            assert payload["stream_options"] == {"include_usage": True}
        assert stream_capture["clients"][0].is_closed


def test_startup_reports_vendor_keys_without_network(tmp_path, monkeypatch):
    from pi.api import server as api

    for name in list(os.environ):
        if name.startswith("PI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("PI_ADMIN_KEY", "synthetic-admin-key-only")
    monkeypatch.setenv("PI_DB_PATH", str(tmp_path / "startup.db"))
    for env_name, _cls, _url in VENDORS.values():
        monkeypatch.setenv(env_name, KEY)
    monkeypatch.setenv("PI_ALLOW_PAID_MODELS", "1")

    def no_network(*args, **kwargs):
        pytest.fail("Startup must not contact external services")

    monkeypatch.setattr(httpx, "post", no_network)
    monkeypatch.setattr(httpx, "get", no_network)

    async def check():
        async with startup.lifespan(api.app):
            assert set(api.app.state.router.adapters()) == {"ollama", *IDS}
            assert api.app.state.provider_credentials == sorted(IDS)
            monkeypatch.setattr(api.app.state.local, "health", lambda: {"status": "ok"})
            status = api.models()["direct"]
            assert set(status) == set(IDS)
            assert all(s["health"]["status"] == "unverified" for s in status.values())
            api._health_cache.clear()
            health = api.health()["checks"]["provider_credentials"]
            assert health["configured"] == sorted(IDS) and health["secrets_included"] is False
            assert KEY not in json.dumps(status) + json.dumps(api.health())

    asyncio.run(check())


@pytest.mark.parametrize("provider_id", IDS)
def test_key_file_is_read_like_the_existing_keys(provider_id, tmp_path):
    env_name = VENDORS[provider_id][0]
    path = tmp_path / provider_id
    path.write_text(KEY + "\n", encoding="ascii")
    assert secrets.load({env_name + "_FILE": str(path)})[env_name] == KEY
    with pytest.raises(ValueError) as error:
        secrets.load({env_name: KEY, env_name + "_FILE": str(path)})
    assert KEY not in str(error.value)
