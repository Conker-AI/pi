"""Direct text adapters. Credentials and spending authority come from server setup.

Catalogue settings select an adapter; they never set its endpoint or credentials.
No discovery, automatic paid fallback, SDK retries, or inferred dollar charges.
"""
from __future__ import annotations

from collections.abc import Mapping

import httpx

from .providers import Completion, Message, ProviderUnavailable, chat_content


def _count(value):
    return value if type(value) is int and value >= 0 else None


class _DirectProvider:
    supports_images = True
    def __init__(self, api_key: str, *, allow_paid: bool = False, timeout: float = 180.0):
        self.api_key = api_key.strip()
        self.allow_paid = allow_paid
        self.timeout = timeout

    def health(self) -> dict:
        if not self.api_key:
            return {"status": "not_configured", "reason": "no API key"}
        if not self.allow_paid:
            return {"status": "not_configured", "reason": "paid models are disabled"}
        return {"status": "unverified", "reason": "configured; no live request performed"}

    def complete(self, messages: list[Message], *, model: str) -> Completion:
        return self.complete_bounded(messages, model=model, timeout=self.timeout)

    def complete_bounded(self, messages: list[Message], *, model: str,
                         timeout: float) -> Completion:
        if not self.api_key or not self.allow_paid:
            raise ProviderUnavailable("direct provider requires a key and paid-model opt-in")
        if not model.strip() or not messages or any(
            m.role not in {"system", "user", "assistant"} or not isinstance(m.content, str)
            for m in messages
        ):
            raise ProviderUnavailable("unsupported direct text request")
        payload = self._payload(messages, model)
        try:
            response = httpx.post(self.url, headers=self._headers(), json=payload,
                                  timeout=timeout, follow_redirects=False)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError()
            return self._completion(body, model)
        except Exception as exc:
            # Provider bodies, URLs, and exception text can contain credentials or
            # prompts. Only the exception class crosses the adapter boundary.
            raise ProviderUnavailable(type(exc).__name__) from None


class OpenAIProvider(_DirectProvider):
    name = "openai"
    url = "https://api.openai.com/v1/chat/completions"

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    def _payload(self, messages, model):
        return {"model": model, "messages": [
            {"role": m.role, "content": chat_content(m)} for m in messages], "store": False}

    def _completion(self, body, model):
        choice = body["choices"][0]
        message = choice["message"]
        text = message.get("content")
        if not isinstance(text, str) or message.get("tool_calls"):
            raise ValueError()
        usage = body.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        actual = body.get("model", model)
        if not isinstance(actual, str) or not actual:
            raise ValueError()
        return Completion(text=text, model=actual, provider=self.name,
                          input_tokens=_count(usage.get("prompt_tokens")),
                          output_tokens=_count(usage.get("completion_tokens")),
                          cached_tokens=_count(details.get("cached_tokens")),
                          raw={"finish_reason": choice.get("finish_reason")})


class AnthropicProvider(_DirectProvider):
    name = "anthropic"
    url = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, *, allow_paid: bool = False,
                 timeout: float = 180.0, max_tokens: int = 4096):
        super().__init__(api_key, allow_paid=allow_paid, timeout=timeout)
        if type(max_tokens) is not int or not 1 <= max_tokens <= 65536:
            raise ValueError("max_tokens must be between 1 and 65536")
        self.max_tokens = max_tokens

    def _headers(self):
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    def _payload(self, messages, model):
        # Only leading system instructions can be lifted without changing order.
        system, turns = [], []
        for message in messages:
            if message.images and message.role != "user":
                raise ProviderUnavailable("Images require a user attachment message.")
            if message.role == "system":
                if turns:
                    raise ProviderUnavailable("system instructions must precede conversation")
                system.append({"type": "text", "text": message.content})
            else:
                content = ([{"type": "image", "source": {"type": "base64",
                    "media_type": image.media_type, "data": image.data}} for image in message.images]
                    + [{"type": "text", "text": message.content}]) if message.images else message.content
                turns.append({"role": message.role, "content": content})
        if not turns:
            raise ProviderUnavailable("direct text request requires conversation messages")
        payload = {"model": model, "messages": turns, "max_tokens": self.max_tokens}
        if system:
            payload["system"] = system
        return payload

    def _completion(self, body, model):
        blocks = body["content"]
        if not isinstance(blocks, list) or not blocks or any(
            not isinstance(b, dict) or b.get("type") != "text"
            or not isinstance(b.get("text"), str) for b in blocks
        ):
            raise ValueError()
        usage = body.get("usage") or {}
        tokens = _count(usage.get("input_tokens"))
        # Anthropic reports uncached, cache creation and cache read separately.
        extras = [_count(usage.get(k, 0)) for k in
                  ("cache_creation_input_tokens", "cache_read_input_tokens")]
        total = tokens + sum(extras) if tokens is not None and None not in extras else None
        actual = body.get("model", model)
        if not isinstance(actual, str) or not actual:
            raise ValueError()
        return Completion(text="".join(b["text"] for b in blocks), model=actual,
                          provider=self.name, input_tokens=total,
                          output_tokens=_count(usage.get("output_tokens")),
                          cached_tokens=_count(usage.get("cache_read_input_tokens")),
                          raw={"finish_reason": body.get("stop_reason")})


def configured(environment: Mapping[str, str], *, timeout: float = 180.0) -> dict:
    """Pure factory: never loads .env, contacts providers, or reads UI key drafts."""
    allowed = environment.get("PI_ALLOW_PAID_MODELS", "").strip() in {"1", "true", "yes"}
    from .decision_provider import configured as decision_providers
    return {**decision_providers(environment), **{adapter.name: adapter(environment[key], allow_paid=allowed, timeout=timeout)
            for key, adapter in (("PI_OPENAI_KEY", OpenAIProvider),
                                 ("PI_ANTHROPIC_KEY", AnthropicProvider))
            if environment.get(key, "").strip()}}
