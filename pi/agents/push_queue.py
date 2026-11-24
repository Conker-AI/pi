"""Which Inbox items may wake the owner's devices (web push). Pi only decides; it sends nothing.

The gateway asks (`POST /push/claim`, owner-control key, never relayed for the browser) and sends
the claimed items to the devices the owner turned on. The rules, in one place:

- Only what the Inbox lists under "Needs you" now (`inbox.needs_you_items`), so a forgotten or
  trashed chat's item, or a private chat's words, never reach this list in the first place.
- Only items that started waiting at or after `after` (when the first device was turned on), so
  turning it on does not replay the backlog.
- The owner's proactivity and quiet hours (`owner_preferences.admission_reasons`): off sends
  nothing, "urgent only" only urgent items, quiet hours hold all but urgent ones the owner let through.
- At most the owner's daily suggestion number of pushes a local day, counted here; they never use up the
  reservations the owner's jobs make (`proactive_budget`).
- One push per item, ever: a claim is recorded before the gateway sends, in the same write
  transaction that checks it, so a retry or a second claim at once never sends it again. A claim
  the gateway could not send is lost, not repeated (the Inbox still shows the item).

- The owner's settings (Conker's settings registry): `notifications.quiet_days` holds every item
  on those days of his local week, urgent ones too; with `notifications.preview` off a claim says
  `"preview": false`, and the gateway then sends only that something needs him.

A held item (quiet hours, quiet day, budget) is not consumed: it may be claimed later while it still waits.
Nothing here keeps a title or any text: a claim is a digest of the item's kind and id and a time.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime

from pydantic import Field

from pi.agents import inbox
from pi.core import owner_preferences as prefs
from pi.core import owner_settings as owner_settings
from pi.core.models import Strict

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_deliveries (
 item_key TEXT PRIMARY KEY CHECK(length(item_key)=64), claimed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS push_deliveries_claimed ON push_deliveries(claimed_at);
"""
ONE = prefs.Usage(suggestions=1, researchMinutes=0, costCents=0)
QUIET_DAYS, PREVIEW = "notifications.quiet_days", "notifications.preview"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class Claim(Strict):
    # Unix seconds: when the first of the owner's devices was turned on (the gateway knows it).
    after: float = Field(ge=0, le=10_000_000_000)
    limit: int = Field(default=5, ge=1, le=20)


def initialize(db) -> None:
    db.executescript(SCHEMA)


def item_key(item: dict) -> str:
    return hashlib.sha256(f"{item['kind']}:{item['id']}".encode()).hexdigest()


def _pushed_today(db, preferences, now: datetime) -> int:
    """Pushes claimed on the owner's current local day, in the owner's zone now (a change of time
    zone cannot reset a day already used)."""
    day = prefs.local_day(preferences, now)
    rows = db.execute(
        "SELECT claimed_at FROM push_deliveries WHERE claimed_at>=?",
        (now.timestamp() - 3 * 86400,),
    )
    return sum(
        prefs.local_day(preferences, datetime.fromtimestamp(row[0], UTC)) == day for row in rows
    )


def _quiet_day(db, day: str) -> bool:
    """Whether the owner's local `day` (an ISO date) is one he keeps free of notifications."""
    days = owner_settings.value_in(db, QUIET_DAYS)
    return isinstance(days, list) and WEEKDAYS[date.fromisoformat(day).weekday()] in days


def claim(store, body: Claim, *, now: datetime | None = None) -> dict:
    body = Claim.model_validate(body.model_dump())
    now = now or datetime.now(UTC)
    claimed, held = [], set()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        # Read the Inbox while holding the write lock: a forget, trash or answer cannot commit
        # between this read and the claim (the reads see committed rows; WAL lets them run).
        moment = now.timestamp()
        waiting = inbox.needs_you_items(store, inbox.local_day_start(store, moment), moment)
        # Unreadable preferences raise: nothing is claimed, so nothing is sent (fail closed).
        row = db.execute("SELECT preferences FROM owner_preferences WHERE singleton=1").fetchone()
        preferences = prefs.OwnerPreferences.model_validate_json(row["preferences"])
        day = prefs.local_day(preferences, now)
        used = _pushed_today(db, preferences, now)
        quiet_day = _quiet_day(db, day)
        preview = owner_settings.value_in(db, PREVIEW) is not False
        for item in waiting:
            if len(claimed) == body.limit:
                break
            if type(item.get("since")) not in (int, float) or item["since"] < body.after:
                continue
            key = item_key(item)
            if db.execute("SELECT 1 FROM push_deliveries WHERE item_key=?", (key,)).fetchone():
                continue
            if quiet_day:
                held.add("quiet_day")
                continue
            urgent = item.get("urgency", {}).get("urgent") is True
            reasons = prefs.admission_reasons(
                preferences,
                now,
                urgent=urgent,
                usage_day=day,
                used=prefs.Usage(suggestions=used + len(claimed), researchMinutes=0, costCents=0),
                proposed=ONE,
            )
            if reasons:
                held.update(reasons)
                continue
            db.execute("INSERT INTO push_deliveries VALUES (?,?)", (key, now.timestamp()))
            shown = {} if preview else {"preview": False}
            claimed.append({"kind": item["kind"], "urgent": urgent, "tag": key[:16], **shown})
        db.commit()
    return {"items": claimed, "held": sorted(held), "delivery": "at-most-once"}
