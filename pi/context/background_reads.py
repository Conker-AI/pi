"""What the Morning check-in's reads of ToolGate's read-only tools share: the calendar
(`pi.context.calendar_events`) and the mail (`pi.context.recent_mail`).

A read is made before the run's turn, through the job agent's own ToolGate client, and only when
it cannot ask the owner (`answer`): the tool is in Pi's catalogue, its authorization is `auto`, it
is not sensitive, the agent's own access for it is "allow", and setup did not leave tools off. A
refusal, a ToolGate that is down, an unknown outcome or a failed result reads nothing. A read that
comes back asking anyway (the owner changed the tool in between) takes its Inbox card back at once:
a background read never asks.

What such a tool returns from outside (a title, a subject, a sender) comes fenced by ToolGate,
`[UNTRUSTED_<KIND>_CONTENT field=<name>]`, the text, `[/UNTRUSTED_<KIND>_CONTENT]` on three lines
(toolgate/core/untrusted_text.py `fenced`). `unfenced` takes the text out of that exact fence, or
gives None for anything else. `quoted` and `quoted_within` write it as one JSON string with
invisible characters removed, whitespace folded and `<` escaped, so it can neither close the
block's fence nor pass for a line of its own.

`section` and `lines` build a block that is bounded as the model reads it, after escaping (a `<`
is six characters there): lines stop before the block passes its limit, and one source repeated
many times shows at most a few times, then how many more there are.
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter

from pi.agents import profiles
from pi.core import setup_choices
from pi.core.toolgate_types import (
    ApprovalRequired,
    ToolGateUnavailable,
    ToolRefused,
    ToolResult,
)
from pi.core.untrusted_text import strip_invisible

NL = chr(10)
# "<" as JSON writes it inside a string: the same value, and a fence cannot be closed early.
ESCAPED_LT = chr(92) + "u003c"


def clean(text: str, chars: int) -> str:
    """One line of what a reader can see: invisible and control characters gone, spaces folded,
    at most `chars` characters."""
    return " ".join(strip_invisible(text).split())[:chars]


def quoted(text: str, chars: int) -> str:
    """`text` cleaned to `chars` characters, as one JSON string with `<` escaped."""
    return json.dumps(clean(text, chars), ensure_ascii=False).replace("<", ESCAPED_LT)


def quoted_within(text: str, limit: int) -> str:
    """`text` as one JSON string with `<` escaped, cut so that it is at most `limit` characters
    as the model reads it, quotes included: a field full of `<` costs no more than plain text."""
    pieces, used = [], 2
    for char in clean(text, limit):  # an escaped character is never shorter than itself
        piece = json.dumps(char, ensure_ascii=False)[1:-1].replace("<", ESCAPED_LT)
        if used + len(piece) > limit:
            break
        pieces.append(piece)
        used += len(piece)
    return '"' + "".join(pieces) + '"'


def unfenced(value, kind: str, field: str) -> str | None:
    """The text inside ToolGate's `kind` fence for `field`, or None when the value is not
    fenced exactly so (another field's or kind's fence, more lines, an open fence)."""
    if not isinstance(value, str):
        return None
    parts = value.split(NL)
    if len(parts) != 3 or parts[0] != f"[UNTRUSTED_{kind}_CONTENT field={field}]":
        return None
    return parts[1] if parts[2] == f"[/UNTRUSTED_{kind}_CONTENT]" else None


def _usable(store, client, agent_id: str, tool_id: str) -> bool:
    if client is None or setup_choices.current(store, "capabilities").choice == "skip":
        return False
    try:
        tool = next((tool for tool in client.tools() if tool.id == tool_id), None)
    except ToolGateUnavailable:
        return False
    if tool is None or tool.authorization != "auto" or tool.sensitive:
        return False
    try:
        access = profiles.get(store, agent_id)["configuration"]["toolAccess"]
    except profiles.AgentError:
        return False
    return access.get(tool_id, "allow") == "allow"


def answer(store, client, tool_id: str, args: dict, *, action_id: str, agent_id: str):
    """The tool's result (a dict) for this read, or None when nothing was read (see the module
    notes); a repeated `action_id` gets ToolGate's saved receipt, never a second read."""
    if not _usable(store, client, agent_id, tool_id):
        return None
    try:
        outcome = client.invoke(tool_id, args, action_id=action_id)
    except (ToolRefused, ToolGateUnavailable):
        return None
    if isinstance(outcome, ApprovalRequired):
        # If ToolGate cannot be reached, the card stays until it expires; nothing runs unanswered.
        with contextlib.suppress(ToolGateUnavailable):
            client.withdraw(outcome.request_id, None)
        return None
    if not (isinstance(outcome, ToolResult) and outcome.ok and isinstance(outcome.result, dict)):
        return None
    return outcome.result


def lines(items, room: int, *, line, alike, more: str, most: int, alike_most: int, alike_more):
    """The items' lines in order, at most `most` items and `room` characters (newlines counted),
    with `more` after them when some did not fit; an item whose `alike` key was seen `alike_most`
    times is then one line, `alike_more(n)`, saying how many more there are."""
    total, seen = Counter(alike(item) for item in items), Counter()
    found, listed = [], 0
    room -= len(more) + 1
    for item in items:
        key = alike(item)
        seen[key] += 1
        if seen[key] > alike_most:
            continue
        unit = [line(item)]
        if seen[key] == alike_most and total[key] > alike_most:
            unit.append(alike_more(total[key] - alike_most))
        cost = sum(len(text) + 1 for text in unit)
        if listed == most or cost > room:
            return [*found, more]
        found, listed, room = [*found, *unit], listed + 1, room - cost
    return found


def section(head: str, fence: str, notes: list[str], limit: int, fill) -> str:
    """`head`, then `fill(room)`'s lines and `notes` inside a `<fence>` block, all of it at most
    `limit` characters: `fill` is given the room the frame leaves."""
    frame = [head, f"<{fence}>", *notes, f"</{fence}>"]
    body = fill(limit - len(NL.join(frame)))
    return NL.join([*frame[:2], *body, *frame[2:]])
