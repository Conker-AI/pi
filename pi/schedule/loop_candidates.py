"""Open-loop candidates in a proposal pass's model reply (R-M7, L2).

The pass already shows the model the owner's own messages under aliases (e1, e2...). When the
caller can file offers, the model may also name promises the owner made in those messages. This
module only reads the reply: it keeps a candidate that cites exactly one supplied alias, so the
words always trace to one owner message. Whether and how it is offered is `pi.context.loop_offers`.
"""

from __future__ import annotations

import json
import re

MAX_PER_PASS = 2
MAX_TEXT = 500

ADDENDUM = """

Also look for promises the owner made in their own messages ("I'll send Maya the invoice \
Friday", "remind me after the exam"): a real commitment to do something, not a wish, a question \
or something they asked you to do. Add them under "openLoops" (always include the key; use [] \
when there are none), at most {limit}: {{"openLoops": [{{"text": "the promise copied word for word from that message, e.g. send Maya \
the invoice", "dueOn": "YYYY-MM-DD only when the words fix a day, else null", "evidence": \
"e3"}}]}}. "evidence" is the one alias of the message that holds the promise."""


def addendum() -> str:
    return ADDENDUM.format(limit=MAX_PER_PASS)


def parse(text: str, evidence: dict[str, dict], limit: int = MAX_PER_PASS) -> list[dict]:
    """Candidates: {"text", "due_on", "message_id", "session_id"}. `evidence` maps an alias to
    {"id", "session_id"}. Anything malformed is dropped; a reply with no loops gives []."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return []
    try:
        items = json.loads(match.group(0)).get("openLoops")
    except (ValueError, AttributeError):
        return []
    if not isinstance(items, list):
        return []
    found, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        words, alias, due = item.get("text"), item.get("evidence"), item.get("dueOn")
        if not isinstance(words, str) or not words.strip() or alias not in evidence:
            continue
        if alias in seen:
            continue
        seen.add(alias)
        source = evidence[alias]
        found.append(
            {
                "text": " ".join(words.split())[:MAX_TEXT],
                "due_on": due if isinstance(due, str) else None,
                "message_id": source["id"],
                "session_id": source["session_id"],
            }
        )
        if len(found) == limit:
            break
    return found
