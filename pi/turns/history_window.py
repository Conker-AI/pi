"""How much of a chat the model is given, from the owner's settings (Conker's settings registry).

Only what the model is given changes; the transcript itself never does (`pi.turns.loop`).

- `context.history`: Auto and Everything send every earlier message, as before. Recent messages
  sends the last `context.recent_messages` of his and Conker's messages (5 to 200, 30 unless set)
  and what came between them; Summary only sends none, so the model has the chat's summary when it
  has one. The current turn's own messages and the message replied to always stay, and a chat with
  its own context rules (`pi.context.controls`) keeps those instead.
- `context.web_keep`: a page read with research.fetch stays for the answer that read it (the
  default); later answers get a short note in its place, unless the chat pins it exactly. For the
  whole chat keeps every page.
- `tools.result_limit`: the most of one tool result the model is given, 1 to 1024 KB (64 unless
  set); the rest is cut, and the model is told.

Every value is checked again here; anything outside the registry's choices counts as unset.
"""

from __future__ import annotations

import json

from pi.core import owner_settings as owner_settings

HISTORY, RECENT, WEB_KEEP, RESULT_LIMIT = (
    "context.history",
    "context.recent_messages",
    "context.web_keep",
    "tools.result_limit",
)
RECENT_DEFAULT, RECENT_RANGE = 30, (5, 200)
LIMIT_DEFAULT, LIMIT_RANGE = 64, (1, 1024)
FETCH_TOOLS = ("research.fetch", "research.fetch-batch")
LEFT_OUT = (
    "A web page read for an earlier answer. The owner keeps web pages for one answer, so its text "
    "is left out here; search again if it is needed."
)


def _whole(value, bounds, default) -> int:
    """The owner's whole number in bounds, or the default for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        return default
    return int(value) if bounds[0] <= value <= bounds[1] else default


def _current(store, turn_id) -> set[str]:
    """The ids of the messages the current turn has added (its input, tool results, steering)."""
    if not turn_id:
        return set()
    with store._connect() as db:
        rows = db.execute("SELECT message_id FROM turn_messages WHERE turn_id=?", (turn_id,))
        return {row[0] for row in rows}


class Window:
    """The owner's three settings, read once for one assembly of a turn's history."""

    def __init__(self, store, turn_id, policy):
        values = owner_settings.load(store)["values"]
        self.history = values.get(HISTORY)
        self.recent = _whole(values.get(RECENT), RECENT_RANGE, RECENT_DEFAULT)
        self.keep_pages = values.get(WEB_KEEP) == "chat"
        self.limit = _whole(values.get(RESULT_LIMIT), LIMIT_RANGE, LIMIT_DEFAULT) * 1024
        self.policy = policy
        self.current = _current(store, turn_id)
        rules = (policy or {}).get("messagePolicies", {})
        self.pinned = {identity for identity, mode in rules.items() if mode == "keep-exact"}

    def rows(self, rows: list[dict], reply_to=None) -> list[dict]:
        """The selected rows the model is given under Chat history."""
        if self.policy or self.history not in ("recent", "summary"):
            return rows
        start = next((i for i, row in enumerate(rows) if row["id"] in self.current), len(rows))
        cut = start
        if self.history == "recent":
            spoken = [
                i for i, row in enumerate(rows[:start]) if row["role"] in ("user", "assistant")
            ]
            cut = spoken[-self.recent] if len(spoken) > self.recent else 0
        return [row for i, row in enumerate(rows) if i >= cut or row["id"] == reply_to]

    def text(self, row: dict, text: str) -> str:
        """One tool result as the model is given it; any other message is unchanged."""
        if row["role"] != "tool":
            return text
        if not self.keep_pages and row["id"] not in self.current | self.pinned:
            try:
                tool = json.loads(text).get("tool")
            except (ValueError, AttributeError):
                tool = None
            if tool in FETCH_TOOLS:
                return json.dumps({"tool": tool, "leftOut": LEFT_OUT})
        encoded = text.encode()
        if len(encoded) <= self.limit:
            return text
        kept = encoded[: self.limit].decode(errors="ignore")
        return (
            kept
            + f"\n[Cut here: the owner lets a tool result hold at most {self.limit // 1024} KB "
            "in context; the rest of this result is not shown.]"
        )
