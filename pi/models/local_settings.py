"""The owner's local model settings, as fields of each request Pi sends Ollama.

Three settings in the owner's settings document (`pi.owner.settings`, ids from Conker's settings
registry) shape every Ollama request: chats, helper roles and the services' `/inference` calls
alike. Hosted providers never read them. They are preferences: a value of the wrong type counts as
unset, one out of range is kept to the range, and an unset one sends nothing, so Ollama's own
setting applies (on a standard install `OLLAMA_KEEP_ALIVE` 24h, `OLLAMA_CONTEXT_LENGTH` from
`CONKER_MODEL_CONTEXT`, 8192 by default, and Ollama's own thread count).

A larger context needs more memory. Pi never asks for more than the server's context length when
it knows it (`PI_OLLAMA_CONTEXT_LENGTH`, or `OLLAMA_CONTEXT_LENGTH` when Pi shares Ollama's
environment), since the installer sizes that to the machine. When Pi does not know it, a context
too large for the machine makes local answers fail (Ollama refuses to load the model, or its
container runs out of memory and restarts); Pi reports that as an unavailable model, as it does any
Ollama failure, and a smaller setting recovers.
"""

from __future__ import annotations

import math
import os
import sqlite3

from pi.owner import settings as owner_settings

KEEP_LOADED = "models.local.keep_loaded"
CONTEXT = "models.local.context"
THREADS = "models.local.threads"
# Keep loaded: minutes after the last use, 5 minutes to a day, or until Ollama restarts.
MIN_KEEP_MINUTES, MAX_KEEP_MINUTES = 5, 1440
UNTIL_RESTART = "until_restart"
# Local context size: tokens the model reads at once.
MIN_CONTEXT, MAX_CONTEXT = 2048, 32768
SERVER_CONTEXT = ("PI_OLLAMA_CONTEXT_LENGTH", "OLLAMA_CONTEXT_LENGTH")


def _number(value) -> int | None:
    """A whole number from a number or a string of digits (a select saves "8192"), else None."""
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(value)


def keep_alive(value):
    """Ollama's `keep_alive`: "<minutes>m" kept to 5 minutes..a day, -1 until Ollama restarts."""
    if value == UNTIL_RESTART:
        return -1
    minutes = _number(value)
    if minutes is None:
        return None
    return f"{min(MAX_KEEP_MINUTES, max(MIN_KEEP_MINUTES, minutes))}m"


def server_context(environ=os.environ) -> int | None:
    """The context length the server's Ollama was given, when Pi's environment says it."""
    for name in SERVER_CONTEXT:
        length = _number(environ.get(name, ""))
        if length is not None and length > 0:
            return length
    return None


def num_ctx(value, environ=os.environ) -> int | None:
    """Ollama's `num_ctx`: kept to 2048..32768 and never above the server's context length."""
    tokens = _number(value)
    if tokens is None:
        return None
    tokens = min(MAX_CONTEXT, max(MIN_CONTEXT, tokens))
    ceiling = server_context(environ)
    return tokens if ceiling is None else min(tokens, ceiling)


def host_cores() -> int:
    """Processor threads Pi may use; the server's Ollama shares the same host on an install."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def num_thread(value) -> int | None:
    """Ollama's `num_thread`: 1..the host's cores; 0, below or unset leaves Ollama's choice."""
    threads = _number(value)
    if threads is None or threads < 1:
        return None
    return min(threads, host_cores())


def request_fields(store) -> dict:
    """The fields to add to an Ollama chat request: only the settings the owner chose.

    A settings document that cannot be read sends nothing, so the answer still comes with
    Ollama's own settings rather than failing over a preference."""
    try:
        values = owner_settings.load(store)["values"]
    except (sqlite3.Error, TypeError, ValueError):
        return {}
    if not isinstance(values, dict):
        return {}
    fields, options = {}, {}
    if (alive := keep_alive(values.get(KEEP_LOADED))) is not None:
        fields["keep_alive"] = alive
    if (context := num_ctx(values.get(CONTEXT))) is not None:
        options["num_ctx"] = context
    if (threads := num_thread(values.get(THREADS))) is not None:
        options["num_thread"] = threads
    if options:
        fields["options"] = options
    return fields
