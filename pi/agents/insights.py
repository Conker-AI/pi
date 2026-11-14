"""Is Conker getting quieter? (R-M11 / R-E13) Counts of what asked for the owner and what did not.

Pure counting over rows Pi already keeps, in two equal windows (the last `days` days and the
`days` before them) so the owner can see a trend. Nothing here reads or returns his words: only
counts and times. A chat that is forgotten or in the Trash (pi.conversation.visibility) counts for
nothing, the same as in every other list.

- `handledWithoutAsking`: actions that ran without asking him (Inbox Handled).
- `asked`: things that needed him: change requests raised, questions Conker asked, notices.
- `approved`, `declined`, `failed`: change requests decided in the window.
- `medianAnswerSeconds`: how long a request or question waited before he answered it.
- `quietShare`: handledWithoutAsking / (handledWithoutAsking + asked); null with neither.

Tool approvals are decided in ToolGate, not here, so they are not counted.
"""

from __future__ import annotations

from typing import Literal

from pi.conversation import visibility
from pi.core.models import Strict

DAY = 86400.0
VISIBLE = f"(session_id IS NULL OR NOT {visibility.hidden_sql('session_id')})"


class QuietWindow(Strict):
    since: float
    until: float
    handledWithoutAsking: int
    asked: int
    changeRequestsRaised: int
    questionsAsked: int
    noticesRaised: int
    approved: int
    declined: int
    failed: int
    questionsAnswered: int
    questionsDismissed: int
    upVotes: int
    downVotes: int
    medianAnswerSeconds: float | None
    quietShare: float | None


class QuietInsights(Strict):
    schemaVersion: Literal[1]
    days: int
    current: QuietWindow
    previous: QuietWindow


def _count(db, table: str, where: str, args: tuple) -> int:
    return db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where} AND {VISIBLE}", args).fetchone()[
        0
    ]


def _has(db, table: str) -> bool:
    return bool(
        db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    )


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 1)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 1)


def _waits(db, table: str, answered: str, span: tuple) -> list[float]:
    rows = db.execute(
        f"SELECT created_at, {answered} FROM {table} "
        f"WHERE {answered} >= ? AND {answered} < ? AND {VISIBLE}",
        span,
    ).fetchall()
    return [max(0.0, row[1] - row[0]) for row in rows]


def _window(db, since: float, until: float) -> dict:
    span = (since, until)
    made = "created_at >= ? AND created_at < ?"
    handled = asked_requests = questions = notices = 0
    approved = declined = failed = answered = dismissed = ups = downs = 0
    waits: list[float] = []
    if _has(db, "handled_actions"):
        handled = _count(db, "handled_actions", made, span)
    if _has(db, "change_requests"):
        decided = "decided_at >= ? AND decided_at < ?"
        asked_requests = _count(db, "change_requests", made, span)
        approved = _count(db, "change_requests", f"state='approved' AND {decided}", span)
        declined = _count(db, "change_requests", f"state='declined' AND {decided}", span)
        failed = _count(db, "change_requests", f"state='failed' AND {decided}", span)
        waits += _waits(db, "change_requests", "decided_at", span)
    if _has(db, "owner_asks"):
        done = "answered_at >= ? AND answered_at < ?"
        questions = _count(db, "owner_asks", made, span)
        answered = _count(db, "owner_asks", f"state='answered' AND {done}", span)
        dismissed = _count(db, "owner_asks", f"state='dismissed' AND {done}", span)
        waits += _waits(db, "owner_asks", "answered_at", span)
    if _has(db, "notices"):
        notices = _count(db, "notices", made, span)
    if _has(db, "response_votes"):
        voted = "updated_at >= ? AND updated_at < ?"
        ups = _count(db, "response_votes", f"vote='up' AND {voted}", span)
        downs = _count(db, "response_votes", f"vote='down' AND {voted}", span)
    asked = asked_requests + questions + notices
    return {
        "since": since,
        "until": until,
        "handledWithoutAsking": handled,
        "asked": asked,
        "changeRequestsRaised": asked_requests,
        "questionsAsked": questions,
        "noticesRaised": notices,
        "approved": approved,
        "declined": declined,
        "failed": failed,
        "questionsAnswered": answered,
        "questionsDismissed": dismissed,
        "upVotes": ups,
        "downVotes": downs,
        "medianAnswerSeconds": _median(waits),
        "quietShare": round(handled / (handled + asked), 3) if handled + asked else None,
    }


def quiet(store, now: float, days: int) -> dict:
    """The last `days` days and the `days` before them, ending at `now`."""
    span = days * DAY
    with store._connect() as db:
        current = _window(db, now - span, now)
        previous = _window(db, now - 2 * span, now - span)
    return {"schemaVersion": 1, "days": days, "current": current, "previous": previous}
