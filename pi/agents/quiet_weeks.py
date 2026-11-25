"""Is Conker getting quieter and more useful? (R-M11 / R-E13) The same counts per week.

Read only, from rows Pi already keeps; no model call, no new table. Each of the last `weeks` ISO
weeks (Monday 00:00 to Monday 00:00 in the owner's time zone, the one his quiet hours use) holds
what Conker raised for him that week, and what has become of it since: a week is the cohort of
what was raised in it, so `actedOn + dismissed + unanswered == raised` for every source. The
current week is last and still running. Only counts and times leave Pi, never his words.

A chat that is forgotten or in the Trash, or made from one (`pi.conversation.visibility`), counts
for nothing, the same as in every other list; restoring it brings its counts back. A proposal is
hidden by the proposal engine's own rule (`proposals.HIDDEN`).

Raised, by source (each dated by when it was filed):
- `suggestions`: proposals the ideas pass made (Inbox "suggestion"). Accepted is acted on;
  declined or "never" is dismissed; open is unanswered.
- `questions`: questions Conker asked him in a chat (`owner_asks`). Answered is acted on;
  dismissed is dismissed; open is unanswered.
- `changeRequests`: what Conker asked to create from a chat (`change_requests`). Approved is
  acted on; declined is dismissed; open is unanswered. Not counted: the owner's own requests (no
  chat), those Conker made at once by his access settings (Keep, `keep_results`: they never asked
  him), promises (Pi does not record whether he or Conker approved one) and requests that failed
  when approved (the same: who approved is not recorded).
- `notices`: plan alerts and AI job notes (`notices`, kinds `plan` and `agent-job`). They have
  no answer, only Dismiss: dismissed, or unanswered (still open, or lapsed unread).

Beside them:
- `approvalsAsked`: approval requests his chats' turns parked on (`turn_approval_requests`, one
  per request), dated by when the turn started: Pi keeps no time for the ask itself, and a turn
  parks within its own run. ToolGate decides them, so their answers are not counted here.
- `pushed`: Inbox items claimed for web push to his devices (`push_deliveries`), dated by the
  claim. A claim keeps only a digest of the item, so an item from a hidden chat is recognised by
  recomputing the digests of every hidden chat's items (`push_queue.item_key`).

Derived:
- `raised`: suggestions + questions + changeRequests + notices.
- `asks`: raised + approvalsAsked, every time Conker reached for his attention (a push is the
  same item again, so it is not added).
- `actedOn`, `dismissed`, `unanswered`: summed over the four sources.
- `usefulShare`: actedOn / (suggestions + questions + changeRequests raised), the items he could
  act on (a notice can only be dismissed); null when there were none.

Not counted, and why: the Inbox's status items ("attention": failed or blocked runs) are derived
live from runs with the continuity feed's own rules, so a weekly count would be a second copy of
them; self-check alerts and update offers come from Pi's health and releases, not from Conker's
choices; "memory changed" notices follow his own correction, and whether one reaches a chat is
decided when the Inbox is read.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pi.agents import push_queue
from pi.conversation import visibility
from pi.core import notices
from pi.core.models import Strict
from pi.schedule import agent_jobs, proposals

ACTED, DISMISSED, UNANSWERED = "actedOn", "dismissed", "unanswered"
SOURCES = ("suggestions", "questions", "changeRequests", "notices")
ANSWERABLE = ("suggestions", "questions", "changeRequests")


def _hidden(column: str) -> str:
    return visibility.hidden_sql(column)


# Each source: rows of (filed at, state) in [since, until), and what each state means.
_QUERIES = {
    "suggestions": (
        (
            "SELECT created_at, state FROM proposals WHERE created_at>=? AND created_at<? "
            f"AND id NOT IN ({proposals.HIDDEN})"
        ),
        {"accepted": ACTED, "declined": DISMISSED, "never": DISMISSED, "open": UNANSWERED},
    ),
    "questions": (
        (
            "SELECT created_at, state FROM owner_asks WHERE created_at>=? AND created_at<? "
            f"AND NOT {_hidden('session_id')}"
        ),
        {"answered": ACTED, "dismissed": DISMISSED, "open": UNANSWERED},
    ),
    "changeRequests": (
        (
            "SELECT created_at, state FROM change_requests WHERE created_at>=? AND created_at<? "
            "AND session_id IS NOT NULL AND kind<>'promise' "
            "AND id NOT IN (SELECT request_id FROM keep_results) "
            f"AND NOT {_hidden('session_id')}"
        ),
        {"approved": ACTED, "declined": DISMISSED, "open": UNANSWERED},
    ),
    "notices": (
        (
            "SELECT created_at, CASE WHEN dismissed_at IS NULL THEN 'open' ELSE 'dismissed' END "
            f"FROM notices WHERE kind IN ('{notices.PLAN}','{notices.AGENT_JOB}') "
            "AND created_at>=? AND created_at<? "
            f"AND (session_id IS NULL OR NOT {_hidden('session_id')})"
        ),
        {"dismissed": DISMISSED, "open": UNANSWERED},
    ),
}
_APPROVALS = (
    "SELECT t.started_at FROM turn_approval_requests a JOIN turns t ON t.id=a.turn_id "
    f"WHERE t.started_at>=? AND t.started_at<? AND NOT {_hidden('t.session_id')}"
)
_PUSHES = "SELECT claimed_at, item_key FROM push_deliveries WHERE claimed_at>=? AND claimed_at<?"
# The Inbox items of hidden chats, as (kind, id) the way `inbox.needs_you_items` names them.
_ATTENTION = (
    "SELECT co.event_id FROM continuity_order co "
    "LEFT JOIN activity_events ae ON ae.id=co.activity_event_id "
    "LEFT JOIN scheduled_runs r ON r.id=co.scheduled_run_id "
    "LEFT JOIN agent_job_sessions j ON j.job_id=r.job_id "
    f"WHERE {_hidden('ae.session_id')} OR {_hidden('j.session_id')}"
)
_HIDDEN_ITEMS = (
    ("approval", f"SELECT id FROM turns WHERE {_hidden('session_id')}"),
    ("change-request", f"SELECT id FROM change_requests WHERE {_hidden('session_id')}"),
    ("ask", f"SELECT id FROM owner_asks WHERE {_hidden('session_id')}"),
    (
        notices.AGENT_JOB,
        f"SELECT id FROM notices WHERE kind='{notices.AGENT_JOB}' AND {_hidden('session_id')}",
    ),
    ("attention", _ATTENTION),
)


class Outcomes(Strict):
    raised: int
    actedOn: int
    dismissed: int
    unanswered: int


class QuietWeek(Strict):
    week: str  # ISO week, e.g. "2026-W48"
    startsAt: float  # Monday 00:00 in the owner's zone
    endsAt: float  # the next Monday 00:00; the current week's is still ahead
    asks: int
    raised: int
    approvalsAsked: int
    pushed: int
    actedOn: int
    dismissed: int
    unanswered: int
    usefulShare: float | None
    suggestions: Outcomes
    questions: Outcomes
    changeRequests: Outcomes
    notices: Outcomes


class QuietWeeks(Strict):
    schemaVersion: Literal[1]
    timeZone: str
    weeks: list[QuietWeek]  # oldest first; the current week last


def _midnight(day: date, zone: ZoneInfo) -> float:
    return datetime.combine(day, datetime.min.time(), zone).timestamp()


def _mondays(now: float, zone: ZoneInfo, count: int) -> list[date]:
    today = datetime.fromtimestamp(now, zone).date()
    monday = today - timedelta(days=today.weekday())
    return [monday - timedelta(weeks=back) for back in range(count - 1, -1, -1)]


def _hidden_pushes(db) -> set[str]:
    """The push digests of every Inbox item that comes from a hidden chat."""
    items = [("suggestion", identity) for identity in proposals.hidden_ids(db)]
    for kind, query in _HIDDEN_ITEMS:
        items += [(kind, row[0]) for row in db.execute(query)]
    return {push_queue.item_key({"kind": kind, "id": identity}) for kind, identity in items}


def _empty() -> dict:
    return {"raised": 0, ACTED: 0, DISMISSED: 0, UNANSWERED: 0}


def _week(day: date, starts: float, ends: float) -> dict:
    year, number, _ = day.isocalendar()
    return {
        "week": f"{year}-W{number:02d}",
        "startsAt": starts,
        "endsAt": ends,
        "approvalsAsked": 0,
        "pushed": 0,
        **{source: _empty() for source in SOURCES},
    }


def _finish(week: dict) -> dict:
    raised = sum(week[source]["raised"] for source in SOURCES)
    acted = sum(week[source][ACTED] for source in SOURCES)
    answerable = sum(week[source]["raised"] for source in ANSWERABLE)
    return {
        **week,
        "asks": raised + week["approvalsAsked"],
        "raised": raised,
        ACTED: acted,
        DISMISSED: sum(week[source][DISMISSED] for source in SOURCES),
        UNANSWERED: sum(week[source][UNANSWERED] for source in SOURCES),
        "usefulShare": round(acted / answerable, 3) if answerable else None,
    }


def quiet_weeks(store, now: float, count: int) -> dict:
    """The last `count` ISO weeks in the owner's zone, the current one included, oldest first."""
    zone_name = agent_jobs.owner_time_zone(store)
    zone = ZoneInfo(zone_name)
    mondays = _mondays(now, zone, count)
    edges = [_midnight(day, zone) for day in [*mondays, mondays[-1] + timedelta(weeks=1)]]
    weeks = [_week(day, edges[i], edges[i + 1]) for i, day in enumerate(mondays)]
    span = (edges[0], edges[-1])

    def bucket(at: float) -> dict:
        return weeks[bisect_right(edges, at) - 1]

    with store._connect() as db:
        db.execute("BEGIN")  # every count reads one snapshot
        for source, (query, meaning) in _QUERIES.items():
            for at, state in db.execute(query, span):
                outcome = meaning.get(state)
                if outcome is not None:
                    counts = bucket(at)[source]
                    counts["raised"] += 1
                    counts[outcome] += 1
        for (at,) in db.execute(_APPROVALS, span):
            bucket(at)["approvalsAsked"] += 1
        pushes = db.execute(_PUSHES, span).fetchall()
        hidden = _hidden_pushes(db) if pushes else set()
        for at, key in pushes:
            if key not in hidden:
                bucket(at)["pushed"] += 1
        db.commit()
    return {"schemaVersion": 1, "timeZone": zone_name, "weeks": [_finish(w) for w in weeks]}
