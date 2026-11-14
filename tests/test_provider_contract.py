"""One contract every model adapter that speaks HTTP must pass (R-A9). Synthetic HTTP only.

Whatever the vendor, an adapter must (1) return the text, the model, and the reported token
counts; (2) report a cut answer so the owner never reads it as whole; (3) turn every refusal and
every malformed answer into `ProviderUnavailable`, with a reason that carries no key, no vendor
body and no prompt; (4) never trust a token count it cannot read as a non-negative integer, since
the daily cap counts them; (5) never follow redirects or read proxy settings. Subscription
adapters (ChatGPT, Claude plan) talk to a local socket and have their own tests.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from pi.models.direct import (
    AnthropicProvider,
    DeepSeekProvider,
    GeminiProvider,
    GroqProvider,
    MistralProvider,
    OpenAIProvider,
    XAIProvider,
)
from pi.models.openrouter import OpenRouterProvider
from pi.models.providers import (
    CUT_NOTE,
    Message,
    OllamaProvider,
    ProviderUnavailable,
    shown_text,
)

KEY = "synthetic-contract-key"
PROMPT = "synthetic prompt text the vendor must never see echoed"
VENDOR_BODY = "synthetic vendor error body"


def openai_style(text="Answer", usage=None, finish="stop"):
    return {
        "model": "actual",
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3} if usage is None else usage,
    }


def anthropic_style(text="Answer", usage=None, finish="end_turn"):
    return {
        "model": "actual",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "max_tokens" if finish == "length" else finish,
        "usage": {"input_tokens": 12, "output_tokens": 3} if usage is None else usage,
    }


def ollama_style(text="Answer", usage=None, finish="stop"):
    return {
        "model": "actual",
        "message": {"content": text},
        "done_reason": finish,
        "prompt_eval_count": 12,
        "eval_count": 3,
        **(usage or {}),
    }


def renamed(usage):
    """The same bad counts under each wire format's own field names."""
    return {
        "openai": {"prompt_tokens": usage, "completion_tokens": usage},
        "anthropic": {"input_tokens": usage, "output_tokens": usage},
        "ollama": {"prompt_eval_count": usage, "eval_count": usage},
    }


def openrouter():
    provider = OpenRouterProvider(KEY, allow_paid=True)
    provider._guard_cost = lambda model: SimpleNamespace(supports_images=True)
    return provider


# name -> (provider factory, wire format, builder of a good answer)
ADAPTERS = {
    "openrouter": (openrouter, "openai", openai_style),
    "openai": (lambda: OpenAIProvider(KEY, allow_paid=True), "openai", openai_style),
    "gemini": (lambda: GeminiProvider(KEY, allow_paid=True), "openai", openai_style),
    "xai": (lambda: XAIProvider(KEY, allow_paid=True), "openai", openai_style),
    "mistral": (lambda: MistralProvider(KEY, allow_paid=True), "openai", openai_style),
    "deepseek": (lambda: DeepSeekProvider(KEY, allow_paid=True), "openai", openai_style),
    "groq": (lambda: GroqProvider(KEY, allow_paid=True), "openai", openai_style),
    "anthropic": (lambda: AnthropicProvider(KEY, allow_paid=True), "anthropic", anthropic_style),
    "ollama": (lambda: OllamaProvider("http://localhost:11434"), "ollama", ollama_style),
}
NAMES = list(ADAPTERS)
MALFORMED = [
    [],
    {},
    {"choices": "x", "content": "x", "message": "x"},
    {"choices": [], "content": [], "message": {}},
    {"choices": [{}], "content": [{}], "message": {"content": None}},
    {"choices": [{"message": {"content": 5}}], "content": [{"type": "text", "text": 5}]},
]


@pytest.fixture
def wire(monkeypatch):
    """Every POST answers `reply`; `sent` keeps what the adapter handed to httpx."""
    state = SimpleNamespace(status=200, body=None, sent=[])

    def post(url, **kwargs):
        state.sent.append((url, kwargs))
        request = httpx.Request("POST", url)
        if isinstance(state.body, (dict, list)) or state.status != 200:
            return httpx.Response(state.status, json=state.body, request=request)
        return httpx.Response(state.status, text=str(state.body), request=request)

    monkeypatch.setattr(httpx, "post", post)

    def fail(*_args, **_kwargs):
        pytest.fail("Provider contract tests use a synthetic HTTP transport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail)
    return state


def ask(name):
    make, _wire, _build = ADAPTERS[name]
    return make().complete([Message("user", PROMPT)], model="some-model")


@pytest.mark.parametrize("name", NAMES)
def test_a_good_answer_carries_text_model_provider_and_reported_counts(name, wire):
    wire.body = ADAPTERS[name][2]()
    done = ask(name)
    assert (done.text, done.model, done.input_tokens, done.output_tokens) == (
        "Answer",
        "actual",
        12,
        3,
    )
    assert done.provider and shown_text(done) == "Answer"
    assert done.cost_usd is None or done.cost_usd >= 0


@pytest.mark.parametrize("name", NAMES)
def test_a_cut_answer_says_so(name, wire):
    wire.body = ADAPTERS[name][2](finish="length")
    assert shown_text(ask(name)).endswith(CUT_NOTE)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_a_refusal_is_a_provider_unavailable_that_leaks_nothing(name, status, wire):
    wire.status, wire.body = status, {"error": VENDOR_BODY, "echo": PROMPT, "key": KEY}
    with pytest.raises(ProviderUnavailable) as raised:
        ask(name)
    reason = str(raised.value) + getattr(raised.value, "reason", "")
    for secret in (KEY, VENDOR_BODY, PROMPT):
        assert secret not in reason


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("body", MALFORMED, ids=lambda b: repr(b)[:40])
def test_a_malformed_answer_is_never_saved_as_one(name, body, wire):
    wire.body = body
    with pytest.raises(ProviderUnavailable):
        ask(name)


@pytest.mark.parametrize("name", NAMES)
def test_an_answer_that_is_not_json_is_unavailable(name, wire):
    wire.body = "<html>gateway error</html>"
    with pytest.raises(ProviderUnavailable) as raised:
        ask(name)
    assert "gateway error" not in str(raised.value)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("bad", [-1, "12", 1.5, True, None])
def test_a_token_count_that_is_not_a_non_negative_integer_is_unknown(name, bad, wire):
    _make, kind, build = ADAPTERS[name]
    wire.body = build(usage=renamed(bad)[kind])
    done = ask(name)
    assert done.input_tokens is None and done.output_tokens is None


@pytest.mark.parametrize("name", NAMES)
def test_requests_never_follow_redirects_or_read_proxy_settings(name, wire):
    wire.body = ADAPTERS[name][2]()
    ask(name)
    [(_url, kwargs)] = wire.sent
    assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
