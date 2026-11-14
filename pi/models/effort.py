"""Answer effort: how hard the answering model thinks, mapped to each provider's own control.

The owner picks light, medium, high or max for a chat (Auto leaves it to the model). Each provider
names this differently, so every adapter asks this module what to send. A provider or model with no
control for it gets nothing: the setting is ignored there, never an error, and the chat's output
settings say so (`applies`). The choice travels to the one answer call only (`roles.dispatch`
binds it for the answer role), never to the routing helper or any other role.

Sources (checked 2026-11-14): OpenAI `reasoning_effort` on Chat Completions; OpenRouter
`reasoning.effort`; Anthropic Messages `output_config.effort`, with the model's own list of
supported levels from `GET /v1/models/{id}` (`capabilities.effort`); Ollama `think` takes
low/medium/high for gpt-oss.
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote

import httpx

LEVELS = ("light", "medium", "high", "max")

# Chat Completions reasoning models: the o-series and gpt-5 (not its chat variants). Not every
# reasoning model takes every level, so light/medium/high are sent and max is the top of those.
# o1-mini and o1-preview are reasoning models that refuse the parameter.
_OPENAI_REASONING = re.compile(r"^(?!o1-(mini|preview))(o[1-9]\d*(-|$)|gpt-5(?!.*chat))")
_OPENAI_LEVEL = {"light": "low", "medium": "medium", "high": "high", "max": "high"}
_OPENROUTER_LEVEL = {"light": "low", "medium": "medium", "high": "high", "max": "xhigh"}
_OLLAMA_REASONING = re.compile(r"^gpt-oss")
_OLLAMA_LEVEL = {"light": "low", "medium": "medium", "high": "high", "max": "high"}
# The Anthropic levels to try for each, best first; the model's own list decides.
_ANTHROPIC_LEVELS = {
    "light": ("low",),
    "medium": ("medium",),
    "high": ("high",),
    "max": ("max", "high"),
}
_ROUTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")

CAPABILITY_URL = "https://api.anthropic.com/v1/models/"
CAPABILITY_TTL = 3600.0
CAPABILITY_FAILED_TTL = 300.0
_capabilities: dict[tuple[str, str], tuple[float, frozenset[str] | None]] = {}


def openai_value(route: str, level: str | None) -> str | None:
    """`reasoning_effort` for an OpenAI reasoning model; None where the model has no such control."""
    if level in _OPENAI_LEVEL and _OPENAI_REASONING.match(route):
        return _OPENAI_LEVEL[level]
    return None


def openrouter_value(level: str | None, supports_reasoning: bool) -> str | None:
    """`reasoning.effort`, only for a model whose catalogue entry lists `reasoning`."""
    return _OPENROUTER_LEVEL.get(level or "") if supports_reasoning else None


def ollama_value(route: str, level: str | None) -> str | None:
    """`think` as a level, only for models that take one (gpt-oss)."""
    if level in _OLLAMA_LEVEL and _OLLAMA_REASONING.match(route):
        return _OLLAMA_LEVEL[level]
    return None


def anthropic_levels(api_key: str, route: str, headers: dict) -> frozenset[str] | None:
    """The effort levels this Claude model supports, from its own capability list; None when it
    has none or the list could not be read (the setting is then ignored). Cached; a failed read
    is remembered briefly so a down service is not asked on every answer."""
    if not _ROUTE.match(route):
        return None
    key = (api_key, route)
    now = time.monotonic()
    cached = _capabilities.get(key)
    if cached is not None and now < cached[0]:
        return cached[1]
    levels: frozenset[str] | None = None
    try:
        response = httpx.get(
            CAPABILITY_URL + quote(route, safe=""),
            headers=headers,
            timeout=5.0,
            follow_redirects=False,
            trust_env=False,
        )
        response.raise_for_status()
        effort = (response.json().get("capabilities") or {}).get("effort") or {}
        if effort.get("supported") is True:
            levels = frozenset(
                name
                for name in ("low", "medium", "high", "max")
                if isinstance(effort.get(name), dict) and effort[name].get("supported") is True
            )
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        pass
    levels = levels or None
    ttl = CAPABILITY_TTL if levels is not None else CAPABILITY_FAILED_TTL
    _capabilities[key] = (now + ttl, levels)
    return levels


def anthropic_value(level: str | None, supported: frozenset[str] | None) -> str | None:
    """`output_config.effort`: the first of the level's choices the model supports."""
    if not supported or level not in _ANTHROPIC_LEVELS:
        return None
    return next((name for name in _ANTHROPIC_LEVELS[level] if name in supported), None)


def applies(provider_id: str, route: str) -> bool | None:
    """Whether effort reaches this model: True, False, or None when Pi only learns it when it
    asks the provider (a Claude model's capability list, an OpenRouter model's parameters)."""
    if provider_id == "openai":
        return bool(_OPENAI_REASONING.match(route))
    if provider_id == "ollama":
        return bool(_OLLAMA_REASONING.match(route))
    if provider_id in {"anthropic", "openrouter"}:
        return None
    return False
