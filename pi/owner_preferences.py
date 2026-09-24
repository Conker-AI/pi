"""Owner configuration and pure admission checks; no scheduler or permission grants.

Budget checks consume caller-supplied, same-local-day usage. They are not an atomic
reservation ledger. A scheduler must supply authoritative usage and reserve work
before dispatch. Idle timeout is stored here; gateway enforcement is separate.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class QuietHours(StrictModel):
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


class DailyBudget(StrictModel):
    suggestions: int = Field(ge=0, le=100)
    researchMinutes: int = Field(ge=0, le=1440)
    costCents: int = Field(ge=0, le=100000)


class OwnerPreferences(StrictModel):
    quietHours: QuietHours
    urgency: Literal["meaningful", "urgent_only", "off"]
    dailyBudget: DailyBudget
    idleTimeoutMinutes: int

    @field_validator("idleTimeoutMinutes")
    @classmethod
    def idle_timeout(cls, value):
        if value not in (0, 5, 15, 30, 60):
            raise ValueError("Choose an available idle timeout.")
        return value


class UpdatePreferences(StrictModel):
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
    from . import proactive_budget

    db.executescript(SCHEMA)
    db.executescript(proactive_budget.SCHEMA)
    db.execute("INSERT OR IGNORE INTO owner_preferences VALUES (1,1,?)", (json.dumps(DEFAULT),))


def load(store):
    with store._connect() as db:
        row = db.execute(
            "SELECT revision,preferences FROM owner_preferences WHERE singleton=1"
        ).fetchone()
    return {"revision": row["revision"], "preferences": json.loads(row["preferences"])}


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


class Usage(StrictModel):
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
