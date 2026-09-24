"""The provider seam.

Pi speaks one internal format and adapters translate. Adapters are the only
place a provider's quirks live, so the turn loop never branches on which model
answered - that is what makes routing a configuration change rather than a
rewrite, and it is why Pi was built rather than adopted (ADR-0001).

This module carries the interface and one adapter, enough to run and test a
turn. The full set - OpenRouter, direct API keys, auto-discovered free models
and the escalation policy - is #28. Keeping them apart is deliberate: a loop
that only ever saw one provider would grow assumptions about it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from . import citations as message_citations
from . import live_stream


@dataclass(frozen=True)
class ImageInput:
    media_type: str
    data: str = field(repr=False)
    attachment_id: str = ""


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    images: tuple[ImageInput, ...] = ()


def require_images(provider, messages):
    images = [image for message in messages for image in message.images]
    if (
        len(images) > 20
        or sum(len(image.data) for image in images) > 34_952_536
        or any(message.images and message.role != "user" for message in messages)
    ):
        raise ProviderUnavailable(
            "Image context exceeds supported limits; review included messages."
        )
    if any(message.images for message in messages) and not getattr(
        provider, "supports_images", False
    ):
        raise ProviderUnavailable("Selected provider adapter cannot receive images.")


def chat_content(message):
    if not message.images:
        return message.content
    if message.role != "user":
        raise ProviderUnavailable("Images require a user attachment message.")
    return [{"type": "text", "text": message.content}] + [
        {"type": "image_url", "image_url": {"url": f"data:{image.media_type};base64,{image.data}"}}
        for image in message.images
    ]


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    # Cost is reported, never inferred. A provider that does not say what a call
    # cost yields None, which the dashboard renders as `unknown` - not as zero,
    # which would read as free.
    cost_usd: float | None = None
    raw: dict = field(default_factory=dict)
    # Explicit supplied evidence only, never inferred from retrieved context.
    citations: list[dict] = field(default_factory=list)

    def __post_init__(self):
        try:
            object.__setattr__(self, "citations", message_citations.normalize(self.citations))
        except ValueError as exc:
            raise ProviderUnavailable("provider returned invalid citation metadata") from exc


class ProviderUnavailable(RuntimeError):
    """The provider could not answer. The caller degrades; it never invents."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Provider(Protocol):
    name: str

    def complete(self, messages: list[Message], *, model: str) -> Completion: ...

    def health(self) -> dict: ...


def _installed(wanted: str, models: list[str]) -> bool:
    """Whether the wanted model is among those Ollama has.

    Ollama reports `qwen3:4b`, and a request for `qwen3` is served by
    `qwen3:latest` - so a bare name matches any tag of it, while an explicit
    tag must match exactly. Being lax here would recreate the bug in miniature.
    """
    if wanted in models:
        return True
    if ":" in wanted:
        return False
    return any(name.split(":", 1)[0] == wanted for name in models)


class OllamaProvider:
    """Local models over Ollama's chat API.

    First adapter because it costs nothing to run and is already on the box, so
    the loop can be exercised end to end without a paid key. It is a real
    provider, not a stub: if the loop works here it works because the seam is
    right, not because a fake was accommodating.
    """

    name = "ollama"
    supports_images = True

    def __init__(self, base_url: str, timeout: float = 120.0, model: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Which model this provider is expected to serve, so health can ask
        # about *that* one rather than about the existence of any model at all.
        self.model = model

    def complete(self, messages: list[Message], *, model: str) -> Completion:
        return self.complete_bounded(messages, model=model, timeout=self.timeout)

    def complete_bounded(
        self, messages: list[Message], *, model: str, timeout: float
    ) -> Completion:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    **({"images": [image.data for image in m.images]} if m.images else {}),
                }
                for m in messages
            ],
            "stream": live_stream.active(),
        }
        try:
            if payload["stream"]:
                body = self._streamed(payload, timeout)
            else:
                response = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=timeout)
                response.raise_for_status()
                body = response.json()
        except Exception as exc:
            raise ProviderUnavailable(f"{type(exc).__name__}") from exc

        text = (body.get("message") or {}).get("content")
        if not isinstance(text, str):
            raise ProviderUnavailable("provider returned no message content")

        return Completion(
            text=text,
            model=body.get("model", model),
            provider=self.name,
            input_tokens=body.get("prompt_eval_count"),
            output_tokens=body.get("eval_count"),
            # Local inference has no per-token price. None, not 0.0: the second
            # would be a claim about money that this adapter cannot make, and
            # `unknown` is the honest rendering.
            cost_usd=None,
            raw={k: body[k] for k in ("total_duration", "done_reason") if k in body},
        )

    def _streamed(self, payload: dict, timeout: float) -> dict:
        """Ollama streams one JSON object per line; rebuild the non-streamed body."""
        live_stream.begin_attempt()
        text = []
        final: dict = {}
        with httpx.stream(
            "POST", f"{self.base_url}/api/chat", json=payload, timeout=timeout
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise ValueError("stream error")
                piece = (chunk.get("message") or {}).get("content") or ""
                if piece:
                    text.append(piece)
                    live_stream.delta(piece)
                if chunk.get("done"):
                    final = chunk
                    break
        if not final:
            raise ValueError("stream ended before completion")
        return {**final, "message": {"role": "assistant", "content": "".join(text)}}

    def health(self) -> dict:
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=5.0)
            response.raise_for_status()
        except Exception as exc:
            return {"status": "unavailable", "reason": type(exc).__name__}
        models = [m.get("name", "") for m in response.json().get("models", [])]
        if not models:
            return {"status": "not_configured", "reason": "no models pulled"}
        # Any model is not the model. An install where the embedding model
        # downloaded and the chat model did not would otherwise report `ok`
        # while being unable to answer a single turn - readiness claimed from a
        # proxy rather than from the thing that actually has to work.
        if self.model and not _installed(self.model, models):
            return {"status": "not_configured", "reason": f"{self.model} is not pulled"}
        return {"status": "ok"}
