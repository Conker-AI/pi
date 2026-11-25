"""Hide secrets from online models: the owner's `privacy.redact` (Settings > Privacy and data).

Every request to a model that leaves this server goes through one wrapper, `Redacting`, which
`pi.api.startup` puts around each adapter that can send one: OpenRouter, the direct-key
providers, the ChatGPT and Claude subscriptions, a decision service that is not on this server's
loopback, and Ollama. Ollama counts as local only at a loopback address or at `ollama`, its
service on this server's own network, and only for a model Ollama itself says it answers
(`OllamaModels`): a cloud model (`gpt-oss:120b-cloud`, `glm-4.6:cloud`, or one copied to any
other name) is answered online. While the setting is on, the wrapper sends a copy of the
messages (system, owner, assistant, tool results, memories, attachment text)
in which private key blocks (`pi.models.redaction_pem`), API keys and tokens, password
assignments and card numbers (`pi.models.redaction_shapes`) are replaced by a fixed marker
("[removed: key]"). The messages it was given, the stored chat and the model's answer, with any
tool request in it, are never changed.

Each secret needs a known shape, so ordinary numbers, dates, phone numbers, git SHAs and UUIDs
stay. A secret without one (a password in a sentence, a token cut between two messages) is not
found. A replacement never takes a quotation mark or half of a JSON escape, so a tool request or
tool result in JSON stays valid JSON. Every search reads the text in one linear pass.

Off unless the owner turns it on (the registry's default). Fail closed: a setting Pi cannot
read, or that holds anything but true or false, counts as on, and a request Pi could not clean
is not sent (a model refusal, so the role moves on to its fallback). How many of each kind were
removed, never what, goes on the call's line in the turn's usage receipt
(`pi.context.research_usage`).
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from urllib.parse import urlsplit

from pi.core import owner_settings as owner_settings
from pi.core.provider_types import Message
from pi.models import redaction_pem, redaction_shapes
from pi.models.providers import NotBilled

log = logging.getLogger(__name__)

SETTING = "privacy.redact"
DEFAULT = False  # the registry's default
MARKERS = {"private_key": redaction_pem.MARKER, **redaction_shapes.MARKERS}
UNSENT = "Pi could not hide the secrets in this request, so it was not sent to an online model."
# Adapter members that send text without `complete`: a decision service's `choose` and
# `rank_memories`, and the raw HTTP client and transport. A wrapped adapter does not hand them out.
UNWRAPPED = frozenset({"choose", "rank_memories", "client", "transport"})
# An Ollama model that Ollama answers online by its name: a `-cloud` name or a `cloud` tag.
CLOUD_MODEL = re.compile(r"(?:^|[-:/])cloud(?=$|:)", re.IGNORECASE)
KEPT_FOR = 300.0  # seconds Ollama's answer about where a model runs is kept

_UNSET = object()
_tally: ContextVar[Counter | None] = ContextVar("redaction_tally", default=None)


def redact(text: str) -> tuple[str, Counter]:
    """The text with every secret it shows replaced by its marker, and how many of each kind."""
    counts: Counter = Counter()
    text, keys = redaction_pem.private_keys(text)
    if keys:
        counts["private_key"] = keys
    text = redaction_shapes.tokens(text, counts)
    text = redaction_shapes.assignments(text, counts)
    text = redaction_shapes.cards(text, counts)
    return text, counts


def outgoing(messages) -> tuple[list, Counter]:
    """A redacted copy of the messages; the messages given are never changed."""
    sent, total = [], Counter()
    for message in messages:
        if isinstance(message, Message) and isinstance(message.content, str):
            text, counts = redact(message.content)
            if counts:
                message = replace(message, content=text)
                total.update(counts)
        sent.append(message)
    return sent, total


def enabled(store) -> bool:
    """Whether the owner hides secrets from online models. Unset follows the registry's default;
    only a stored false turns it off, so a setting Pi cannot read, or any other value, counts
    as on."""
    try:
        value = owner_settings.value(store, SETTING, _UNSET)
    except Exception:
        log.warning("%s could not be read; secrets are hidden from online models", SETTING)
        return True
    return DEFAULT if value is _UNSET else value is not False


@contextmanager
def tally(counts: Counter) -> Iterator[Counter]:
    """Add to `counts` what the online calls made in this block had removed, for their receipt."""
    saved = _tally.set(counts)
    try:
        yield counts
    finally:
        _tally.reset(saved)


class Redacting:
    """An adapter that is sent only the redacted copy of each request that leaves this server
    while the owner's setting is on; everything else reads through to the adapter. `stays` says
    which models answer on this server (none, unless given)."""

    def __init__(self, adapter, store, stays: Callable[[str], bool] | None = None) -> None:
        self._adapter = adapter
        self._store = store
        self._stays = stays

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)  # never recurse before __init__ ran (copy, pickle)
        if name in UNWRAPPED:
            raise AttributeError(f"{name} would send text past redaction")
        return getattr(self._adapter, name)

    def complete(self, messages, *, model: str):
        return self._adapter.complete(self._outgoing(messages, model), model=model)

    def complete_bounded(self, messages, *, model: str, timeout: float):
        sent = self._outgoing(messages, model)
        return self._adapter.complete_bounded(sent, model=model, timeout=timeout)

    def _outgoing(self, messages, model):
        if not enabled(self._store) or (self._stays is not None and self._stays(model)):
            return messages
        try:
            sent, counts = outgoing(messages)
        except Exception as exc:
            # Fail closed: a request Pi could not clean is not sent; the role moves on.
            log.warning("redaction failed (%s); the request was not sent", type(exc).__name__)
            raise NotBilled(UNSENT) from None
        seen = _tally.get()
        if counts and seen is not None:
            seen.update(counts)
        return sent


def on_this_server(url, service: str | None = None) -> bool:
    """Whether a URL's host is this server: a loopback address, or `service`, a service's name
    on this server's own network. Anything else, or a URL that cannot be read, is not."""
    try:
        host = urlsplit(url).hostname if isinstance(url, str) else None
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or (service is not None and host == service):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OllamaModels:
    """Which models an Ollama on this server answers itself. Ollama answers a cloud model online
    under any name it is given, so a model is local only when Ollama says so (`remote`, its
    /api/show answer). Each name is asked once and the answer kept for `KEPT_FOR`; a cloud name
    is online without asking, and a model Ollama does not know, or a question it could not
    answer, counts as online and is asked again next time."""

    def __init__(self, adapter) -> None:
        self._adapter = adapter
        self._answers: dict[str, tuple[bool, float]] = {}

    def __call__(self, model) -> bool:
        if not isinstance(model, str) or CLOUD_MODEL.search(model):
            return False
        now = time.monotonic()
        kept = self._answers.get(model)
        if kept is not None and now - kept[1] < KEPT_FOR:
            return kept[0]
        try:
            local = self._adapter.remote(model) is False
        except Exception as exc:
            log.warning("Ollama could not say where %s runs (%s)", model, type(exc).__name__)
            return False
        if len(self._answers) >= 256:
            self._answers.clear()
        self._answers[model] = (local, now)
        return local


def wrap(store, local, hosted, providers: dict) -> tuple:
    """Ollama, OpenRouter and every other adapter that can send a request off this server,
    behind the owner's setting. A decision service on loopback answers here and stays as it is."""

    def guarded(adapter):
        if adapter is None:
            return None
        name = getattr(adapter, "name", None)
        if name == "decisions" and on_this_server(getattr(adapter, "url", None)):
            return adapter
        if name == "ollama" and on_this_server(getattr(adapter, "base_url", None), "ollama"):
            return Redacting(adapter, store, OllamaModels(adapter))
        return Redacting(adapter, store)

    return (
        guarded(local),
        guarded(hosted),
        {name: guarded(adapter) for name, adapter in providers.items()},
    )
