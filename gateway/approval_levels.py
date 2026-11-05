"""How much an owner decision on a ToolGate request must prove. ToolGate decides; the page never does.

ToolGate works out each request's level when the request is made, from the tool definitions it
holds (his Inbox card for the tool, the tool's name, what it runs, every tool a workflow runs) and
seals it into the request's fingerprint, so a changed level makes the request unreviewable.

Level 1, normal (do, run, save) and level 2, outward (send, publish, ship): his signed-in session
(cookie, origin, CSRF). The page adds the "Are you sure?" for level 2. Level 3, serious (delete,
pay): also a fresh single-use password proof bound to the exact decision. A no or a dismissal runs
nothing, so it needs the session only. Anything the gateway cannot read (ToolGate down, an older
ToolGate without the field, an odd answer) is level 3: it fails closed to the password.
"""

from __future__ import annotations

NORMAL, OUTWARD, SERIOUS = 1, 2, 3
LEVELS = frozenset({NORMAL, OUTWARD, SERIOUS})


def from_view(identity: str, status_code: int, view: object) -> int:
    """The level ToolGate's owner view of request `identity` states; anything unreadable is serious."""
    if status_code != 200 or not isinstance(view, dict) or view.get("id") != identity:
        return SERIOUS
    value = view.get("approval_level")
    return value if type(value) is int and value in LEVELS else SERIOUS


def needs_password(status: str, level: int) -> bool:
    """Only a yes at the serious level asks for the password again."""
    return status == "approved" and level not in (NORMAL, OUTWARD)
