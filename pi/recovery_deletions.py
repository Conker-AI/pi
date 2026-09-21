"""Offline replay of retained Pi forgetting receipts; never promotes a recovery."""

import argparse
import hashlib
import json
from contextlib import closing
from pathlib import Path

from . import forgetting
from .access import acquire

HOLD = "CREATE TABLE IF NOT EXISTS recovery_deletion_hold (id INTEGER PRIMARY KEY CHECK(id=1))"


def _plan(source, target):
    for db in (source, target):
        if db.execute("PRAGMA integrity_check").fetchall()[0][0] != "ok":
            raise forgetting.ForgettingError("Recovery database integrity check failed.")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise forgetting.ForgettingError("Recovery database references are inconsistent.")
    newer = {
        row["id"]: row["created_at"] for row in source.execute("SELECT id,created_at FROM sessions")
    }
    restored = {
        row["id"]: row["created_at"] for row in target.execute("SELECT id,created_at FROM sessions")
    }
    if not restored or any(newer.get(key) != value for key, value in restored.items()):
        raise forgetting.ForgettingError(
            "The supplied source does not cover the restored sessions."
        )
    deleted = sorted(
        row[0]
        for row in source.execute(
            "SELECT f.session_id FROM forgotten_sessions f "
            "JOIN forgetting_receipts r ON r.id=f.receipt_id"
        )
    )
    if source.execute("SELECT 1 FROM forgetting_maintenance").fetchone():
        raise forgetting.ForgettingError("Finish source forgetting maintenance before replay.")
    value = {
        "restoredSessions": sorted(restored),
        "deletedSessions": deleted,
        "coverage": "supplied-source-only",
        "promotesRecovery": False,
    }
    value["confirmation"] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def preview(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise forgetting.ForgettingError("Choose a separate recovery database.")
    with (
        closing(forgetting._connect(source, readonly=True)) as newer,
        closing(forgetting._connect(target, readonly=True)) as restored,
    ):
        newer.execute("BEGIN")
        restored.execute("BEGIN")
        return _plan(newer, restored)


def replay(source, target, confirmation):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise forgetting.ForgettingError("Choose a separate recovery database.")
    # Hold both process leases, including between individual redaction commits.
    with (
        closing(acquire(source, exclusive=True)),
        closing(acquire(target, exclusive=True)),
        closing(forgetting._connect(source, readonly=True)) as newer,
        closing(forgetting._connect(target)) as restored,
    ):
        newer.execute("BEGIN")
        plan = _plan(newer, restored)
        if confirmation != plan["confirmation"]:
            raise forgetting.ForgettingError("Recovery scope changed; review a new preview.")
        restored.execute(HOLD)
        restored.execute("INSERT OR IGNORE INTO recovery_deletion_hold VALUES (1)")
        restored.execute("PRAGMA secure_delete=ON")
        applied = []
        deleted = set(plan["deletedSessions"])
        for identity in plan["deletedSessions"]:
            if identity not in plan["restoredSessions"]:
                continue
            scope = forgetting._plan(restored, identity)
            if "already_forgotten" in scope:
                continue
            if not set(scope["session_ids"]).issubset(deleted):
                raise forgetting.ForgettingError(
                    "Restored dependencies exceed the supplied deletion evidence."
                )
            forgetting._redact(restored, scope)
            applied.extend(scope["session_ids"])
        forgetting._scrub(restored)
        # Deliberately retained: MemoryGate deletions, external-effect receipts and
        # newer revocations still need coordinated reconciliation before startup.
        return {
            "appliedSessionIds": sorted(applied),
            "recoveryHeld": True,
            "coverage": "supplied-source-only",
            "promotesRecovery": False,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preview", "replay"))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--confirm")
    args = parser.parse_args()
    try:
        result = (
            preview(args.source, args.target)
            if args.operation == "preview"
            else replay(args.source, args.target, args.confirm)
        )
        print(json.dumps(result))
        return 0
    except Exception:
        print("Recovery deletion replay failed; retain the hold and inspect the source databases.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
