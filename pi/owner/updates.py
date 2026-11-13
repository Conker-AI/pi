"""Conker updates for the Inbox: which newer release the host found, and how each update ended.

Pi reads nothing outside (ADR-0005). The host checks the latest Conker release and runs updates
(ADR-0014); with the recovery credential it hands Pi a report: the running version, the newest
release and the receipts of updates that finished. Pi keeps one offer per version, so a dismissed
version stays dismissed and a version at or below the running one closes, and one result per
update receipt, so a report sent twice changes nothing. An open offer newer than the latest
release was pulled after it was found: it is withdrawn (its card goes, and the host would refuse
to install it anyway). Nothing here updates anything.
"""

from __future__ import annotations

import re
import time
from typing import Literal

from pydantic import Field

from pi.core.errors import ServiceError
from pi.core.models import Strict

VERSION = r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$"
RESULT_ID = r"^[0-9a-f]{32}$"
RELEASE_URL = (
    r"^https://github\.com/Conker-AI/conker/releases/tag/v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$"
)
# How each ending reads in the Inbox. A held update needs a restore from the snapshot.
TITLES = {
    "applied": "Updated to {version}",
    "rolled_back": "Update to {version} rolled back",
    "held": "Update to {version} stopped: restore needed",
    "failed": "Update to {version} did not start",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS update_offers (
    version TEXT PRIMARY KEY,
    notes TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT,
    found_at REAL NOT NULL,
    dismissed_at REAL,
    installed_at REAL
);
CREATE TABLE IF NOT EXISTS update_results (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('applied', 'rolled_back', 'held', 'failed')),
    reason TEXT,
    finished_at REAL NOT NULL,
    recorded_at REAL NOT NULL,
    dismissed_at REAL
);
"""


class UpdateError(ServiceError):
    pass


class Release(Strict):
    """The newest published Conker release, as the host read it."""

    version: str = Field(pattern=VERSION)
    notes: str = Field(max_length=20000)
    url: str = Field(max_length=200, pattern=RELEASE_URL)
    publishedAt: str | None = Field(default=None, max_length=40)


class Result(Strict):
    """One finished update: its receipt id, the version it went to and how it ended."""

    id: str = Field(pattern=RESULT_ID)
    version: str = Field(pattern=VERSION)
    outcome: Literal["applied", "rolled_back", "held", "failed"]
    reason: str | None = Field(default=None, max_length=300)
    finishedAt: float = Field(ge=0)


class Report(Strict):
    """What the host found. `running` is None for a build that is not a release."""

    running: str | None = Field(default=None, pattern=VERSION)
    latest: Release | None = None
    results: list[Result] = Field(default_factory=list, max_length=20)


def initialize(db):
    db.executescript(SCHEMA)


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _clock(now: float | None) -> float:
    return time.time() if now is None else now


def record(store, report: Report, *, now: float | None = None) -> dict:
    """Keep what the host found. A known version keeps its row, dismissal included."""
    moment = _clock(now)
    with store._connect() as db:
        if report.running is not None:
            open_rows = db.execute(
                "SELECT version FROM update_offers WHERE installed_at IS NULL"
            ).fetchall()
            for row in open_rows:
                if _key(row["version"]) <= _key(report.running):
                    db.execute(
                        "UPDATE update_offers SET installed_at=? WHERE version=?",
                        (moment, row["version"]),
                    )
        latest = report.latest
        if latest is not None:
            for row in db.execute(
                "SELECT version FROM update_offers WHERE installed_at IS NULL"
            ).fetchall():
                if _key(row["version"]) > _key(latest.version):
                    db.execute("DELETE FROM update_offers WHERE version=?", (row["version"],))
        if latest is not None and (
            report.running is None or _key(latest.version) > _key(report.running)
        ):
            db.execute(
                "INSERT OR IGNORE INTO update_offers VALUES (?,?,?,?,?,NULL,NULL)",
                (latest.version, latest.notes, latest.url, latest.publishedAt, moment),
            )
        for result in report.results:
            db.execute(
                "INSERT OR IGNORE INTO update_results VALUES (?,?,?,?,?,?,NULL)",
                (
                    result.id,
                    result.version,
                    result.outcome,
                    result.reason,
                    result.finishedAt,
                    moment,
                ),
            )
            if result.outcome == "applied":
                db.execute(
                    "UPDATE update_offers SET installed_at=? "
                    "WHERE version=? AND installed_at IS NULL",
                    (moment, result.version),
                )
        db.commit()
    return overview(store)


def _offer(store) -> dict | None:
    """The newest version not yet installed, unless the owner skipped it.

    An older open version is passed over, also once the newest is skipped: skipping 0.10.0 never
    brings back a 0.2.0 card the owner did not see.
    """
    with store._connect() as db:
        rows = db.execute("SELECT * FROM update_offers WHERE installed_at IS NULL").fetchall()
    if not rows:
        return None
    row = max(rows, key=lambda item: _key(item["version"]))
    if row["dismissed_at"] is not None:
        return None
    return {
        "version": row["version"],
        "title": f"Update available: {row['version']}",
        "notes": row["notes"],
        "url": row["url"],
        "publishedAt": row["published_at"],
        "foundAt": row["found_at"],
    }


def _result(row) -> dict:
    return {
        "id": row["id"],
        "version": row["version"],
        "outcome": row["outcome"],
        "title": TITLES[row["outcome"]].format(version=row["version"]),
        "reason": row["reason"],
        "finishedAt": row["finished_at"],
    }


def _results(store, where: str, arguments: tuple = ()) -> list[dict]:
    with store._connect() as db:
        rows = db.execute(
            f"SELECT * FROM update_results WHERE {where} ORDER BY finished_at DESC LIMIT 20",
            arguments,
        ).fetchall()
    return [_result(row) for row in rows]


def overview(store) -> dict:
    """The offer to show, if any, and every result the owner has not dismissed, newest first."""
    return {"offer": _offer(store), "results": _results(store, "dismissed_at IS NULL")}


def needs_you(store) -> list[dict]:
    """For the Inbox: the open offer, and every update that did not end applied."""
    items = []
    offer = _offer(store)
    if offer is not None:
        items.append(
            {
                "kind": "update",
                "id": f"update_{offer['version']}",
                "title": offer["title"],
                "since": offer["foundAt"],
                "version": offer["version"],
                "notes": offer["notes"],
                "url": offer["url"],
                "source": {"kind": "update-offer", "version": offer["version"]},
            }
        )
    for result in _results(store, "dismissed_at IS NULL AND outcome != 'applied'"):
        items.append(
            {
                "kind": "update-result",
                "id": result["id"],
                "title": result["title"],
                "since": result["finishedAt"],
                "outcome": result["outcome"],
                "reason": result["reason"],
                "source": {"kind": "update-result", "resultId": result["id"]},
            }
        )
    return items


def applied(store, since: float) -> list[dict]:
    """Updates that finished applied since a moment (the owner's day), for the handled list."""
    return [
        {
            "kind": "update-applied",
            "title": result["title"],
            "at": result["finishedAt"],
            "source": {"kind": "update-result", "resultId": result["id"]},
        }
        for result in _results(
            store, "dismissed_at IS NULL AND outcome = 'applied' AND finished_at >= ?", (since,)
        )
    ]


def dismiss_offer(store, version: str, *, now: float | None = None) -> dict:
    """Not this version. A newer release still makes a new offer."""
    if not re.fullmatch(VERSION, version):
        raise UpdateError("invalid_version", "Name a version as X.Y.Z.", 422)
    with store._connect() as db:
        cursor = db.execute(
            "UPDATE update_offers SET dismissed_at=? WHERE version=? AND dismissed_at IS NULL",
            (_clock(now), version),
        )
        if (
            not cursor.rowcount
            and not db.execute("SELECT 1 FROM update_offers WHERE version=?", (version,)).fetchone()
        ):
            raise UpdateError("not_found", "No update offer for that version.", 404)
        db.commit()
    return {"version": version, "dismissed": True}


def dismiss_result(store, identity: str, *, now: float | None = None) -> dict:
    if not re.fullmatch(RESULT_ID, identity):
        raise UpdateError("invalid_result", "Name an update result by its id.", 422)
    with store._connect() as db:
        cursor = db.execute(
            "UPDATE update_results SET dismissed_at=? WHERE id=? AND dismissed_at IS NULL",
            (_clock(now), identity),
        )
        if (
            not cursor.rowcount
            and not db.execute("SELECT 1 FROM update_results WHERE id=?", (identity,)).fetchone()
        ):
            raise UpdateError("not_found", "No update result with that id.", 404)
        db.commit()
    return {"id": identity, "dismissed": True}
