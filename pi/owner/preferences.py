"""Owner configuration and pure admission checks; no scheduler or permission grants.

Budget checks consume caller-supplied, same-local-day usage. They are not an atomic
reservation ledger. A scheduler must supply authoritative usage and reserve work
before dispatch. The gateway enforces the idle timeout from the documents it relays
(`gateway/store.py`), never past the host's GATEWAY_IDLE_TIMEOUT_SECONDS.

Saving asks for the password at the gateway, so these preferences may also hold a setting
that lets Conker do more: the access a chat the owner starts begins with (`new_chat_access`).
It changes no chat that exists and grants nothing by itself.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from pi.core.models import Strict


class QuietHours(Strict):
    enabled: bool
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timeZone: str = Field(min_length=1, max_length=100)
    urgentExceptions: bool

    @field_validator("timeZone")
    @classmethod
    def zone(cls, value):
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Provide an available IANA time zone.") from exc
        return value

    @model_validator(mode="after")
    def distinct_times(self):
        if self.enabled and self.start == self.end:
            raise ValueError("Enabled quiet hours need different start and end times.")
        return self


class DailyBudget(Strict):
    suggestions: int = Field(ge=0, le=100)
    researchMinutes: int = Field(ge=0, le=1440)
    costCents: int = Field(ge=0, le=100000)


class PlanAlerts(Strict):
    enabled: bool
    percent: int = Field(ge=50, le=100)


# A chat's access switch (`pi.turns.access.CHAT_LEVELS`): the agent's setting, ask me, AI decides,
# full access.
CHAT_ACCESS = ("default", "ask", "ai", "full")


class OwnerPreferences(Strict):
    quietHours: QuietHours
    urgency: Literal["meaningful", "urgent_only", "off"]
    dailyBudget: DailyBudget
    # Minutes a browser stays signed in after the password; 0 keeps the host's limit.
    idleTimeoutMinutes: int
    # Added 2026-10-21; preferences saved before then read the default.
    planAlerts: PlanAlerts = Field(default_factory=lambda: PlanAlerts(enabled=True, percent=80))
    # Added 2026-10-29: the access switch a chat the owner starts begins with.
    newChatAccess: Literal["default", "ask", "ai", "full"] = "default"

    @field_validator("idleTimeoutMinutes")
    @classmethod
    def idle_timeout(cls, value):
        if value not in (0, 5, 15, 30, 60):
            raise ValueError("Choose an available idle timeout.")
        return value


class UpdatePreferences(Strict):
    expected_revision: int = Field(ge=1)
    preferences: OwnerPreferences


DEFAULT = {
    "quietHours": {
        "enabled": True,
        "start": "22:00",
        "end": "07:00",
        "timeZone": "Asia/Jerusalem",
        "urgentExceptions": False,
    },
    "urgency": "meaningful",
    "dailyBudget": {"suggestions": 4, "researchMinutes": 30, "costCents": 0},
    "idleTimeoutMinutes": 15,
    "planAlerts": {"enabled": True, "percent": 80},
    "newChatAccess": "default",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_preferences (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    revision INTEGER NOT NULL CHECK(revision>=1),
    preferences TEXT NOT NULL
);
"""


class RevisionConflict(Exception):
    def __init__(self, current_revision):
        self.current_revision = current_revision
        super().__init__("Preferences changed. Reload before saving.")


def initialize(db):
    db.executescript(SCHEMA)
    db.execute("INSERT OR IGNORE INTO owner_preferences VALUES (1,1,?)", (json.dumps(DEFAULT),))


def load(store):
    with store._connect() as db:
        row = db.execute(
            "SELECT revision,preferences FROM owner_preferences WHERE singleton=1"
        ).fetchone()
    stored = json.loads(row["preferences"])
    # A setting added after these preferences were saved reads as its default.
    return {
        "revision": row["revision"],
        "preferences": {key: value for key, value in DEFAULT.items() if key not in stored} | stored,
    }


def save(store, request: UpdatePreferences):
    # Revalidate even constructed models: storage must not admit bypassed fields.
    request = UpdatePreferences.model_validate(request.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        revision = db.execute(
            "SELECT revision FROM owner_preferences WHERE singleton=1"
        ).fetchone()[0]
        if revision != request.expected_revision:
            raise RevisionConflict(revision)
        payload = request.preferences.model_dump()
        db.execute(
            "UPDATE owner_preferences SET revision=?,preferences=? WHERE singleton=1",
            (revision + 1, json.dumps(payload)),
        )
        db.commit()
    return {"revision": revision + 1, "preferences": payload}


def new_chat_access(db) -> str:
    """The access switch a chat the owner starts begins with, read in the caller's transaction.

    For a new chat he starts only (the chat route, a draft started as a chat), never an AI job's
    chat or a fork, which copies its parent. Saved with the chat, so a later change leaves chats
    that exist as they are. Anything missing or unreadable starts at "default", which loosens
    nothing.
    """
    row = db.execute("SELECT preferences FROM owner_preferences WHERE singleton=1").fetchone()
    try:
        value = json.loads(row[0]).get("newChatAccess") if row else None
    except (TypeError, ValueError, AttributeError):
        return "default"
    return value if isinstance(value, str) and value in CHAT_ACCESS else "default"


def local_day(preferences: OwnerPreferences, instant: datetime) -> str:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Supply a timezone-aware instant.")
    return instant.astimezone(ZoneInfo(preferences.quietHours.timeZone)).date().isoformat()


def quiet_now(preferences: OwnerPreferences, instant: datetime) -> bool:
    local_day(preferences, instant)  # Reject ambiguous naive timestamps even when disabled.
    quiet = preferences.quietHours
    if not quiet.enabled:
        return False
    clock = instant.astimezone(ZoneInfo(quiet.timeZone)).strftime("%H:%M")
    if quiet.start < quiet.end:
        return quiet.start <= clock < quiet.end
    return clock >= quiet.start or clock < quiet.end


class Usage(Strict):
    suggestions: int = Field(ge=0)
    researchMinutes: int = Field(ge=0)
    costCents: int = Field(ge=0)


def admission_reasons(
    preferences: OwnerPreferences,
    instant: datetime,
    *,
    urgent: bool,
    usage_day: str,
    used: Usage,
    proposed: Usage,
) -> list[str]:
    """Pure policy result, never authorization. Empty means these limits allow it.

    Urgent exceptions bypass quiet hours only, never off or daily ceilings.
    Zero ceilings reject positive requests while permitting zero-cost work.
    DST repeats/skips follow the wall clock in the configured IANA zone.
    """
    if type(urgent) is not bool:
        raise ValueError("Urgency must be explicit.")
    if usage_day != local_day(preferences, instant):
        raise ValueError("Usage must be for the configured local calendar day.")
    reasons = []
    if preferences.urgency == "off":
        reasons.append("proactivity_off")
    elif preferences.urgency == "urgent_only" and not urgent:
        reasons.append("urgent_only")
    if quiet_now(preferences, instant) and not (urgent and preferences.quietHours.urgentExceptions):
        reasons.append("quiet_hours")
    for field, limit in preferences.dailyBudget.model_dump().items():
        if getattr(used, field) + getattr(proposed, field) > limit:
            reasons.append("daily_budget_" + field)
    return reasons
