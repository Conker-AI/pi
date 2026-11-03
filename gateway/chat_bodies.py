"""Typed bodies for the chat feature routes the owner's browser reaches through the control relay.

Each write in ``CHAT_WRITES`` is checked here against the same strict model Pi validates it with,
before the gateway asks for the operation's password proof: an unknown field, a wrong type or a
query string is refused at the gateway and never reaches Pi. ``BODIES`` pairs the contract's writes
with their models in order; a write added to the contract without a model here stops the gateway
from importing (``zip(strict=True)``), so nothing is ever relayed unchecked.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl

from pydantic import ValidationError

from pi.api.browser_contract import CHAT_READS, CHAT_WRITES
from pi.context import compaction, controls, decisions, summaries, summary_draft
from pi.conversation import (
    fact_checks,
    forks,
    judgements,
    naming,
    output_settings,
    retries,
    versions,
)

from .store import AuthError

BODIES = dict(
    zip(
        CHAT_WRITES,
        (
            output_settings.Update,
            naming.Rename,
            naming.AutoName,
            retries.Retry,
            fact_checks.Check,
            forks.Fork,
            versions.Selection,
            judgements.Judge,
            judgements.Vote,
            controls.Update,
            controls.ReviewedFork,
            summaries.Edit,
            summaries.Restore,
            summary_draft.Request,
            compaction.Compact,
            decisions.Add,
            decisions.State,
        ),
        strict=True,
    )
)
# Branch history pages through the chat; no other chat read takes a query.
PAGED = {
    pattern: {"limit", "cursor"} for pattern in CHAT_READS if pattern.endswith("/branch-history")
}


def check(method: str, target: str, query: bytes, body: dict | None) -> None:
    """Refuse a chat feature call whose query or body Pi's own model would not accept."""
    if method == "GET":
        for pattern in CHAT_READS:
            if re.fullmatch(pattern, target):
                _query(query, PAGED.get(pattern, set()))
        return
    for pattern in CHAT_WRITES:
        if re.fullmatch(pattern, target):
            _query(query, set())
            try:
                BODIES[pattern].model_validate_json(json.dumps(body))
            except (ValidationError, ValueError, TypeError):
                raise AuthError(
                    "This request does not match the operation; nothing was sent.", 422
                ) from None
            return


def _query(query: bytes, allowed: set[str]) -> None:
    try:
        pairs = parse_qsl(query.decode("ascii"), keep_blank_values=True, strict_parsing=bool(query))
    except (UnicodeDecodeError, ValueError):
        raise AuthError("Invalid query parameters.", 422) from None
    names = [name for name, _ in pairs]
    if len(names) != len(set(names)) or not set(names) <= allowed:
        raise AuthError("This operation does not accept these query parameters.", 422)
