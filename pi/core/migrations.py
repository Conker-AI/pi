"""Pi's migration ledger: named, ordered upgrade steps, each run once and recorded.

Idempotent setup (`CREATE ... IF NOT EXISTS`) still runs on every start. A migration is a step
that changes what an older Pi left behind: a column it lacked, a CHECK too narrow for a new value.
Each runs at most once per database, in one transaction with its row in `schema_migrations`, so a
failure leaves neither the change nor the record and the next start tries again.

The ledger also stops a downgrade. An id this code does not know was written by a newer Pi whose
schema this code may misread, so Pi refuses to start instead of writing into it.

Rules for a new migration: append it to the list (never rename, reorder or remove an id), and keep
its body safe on a database that already has the change, because a database from before the ledger
may hold part of it.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

LEDGER = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "id TEXT PRIMARY KEY, applied_at REAL NOT NULL, pi_version TEXT NOT NULL)"
)


class UnknownMigration(RuntimeError):
    """The database records a migration this Pi does not know: a newer Pi upgraded it."""


@dataclass(frozen=True)
class Migration:
    id: str
    run: Callable[[sqlite3.Connection], None]
    # A table rebuild copies rows with foreign keys unchecked; SQLite only changes that setting
    # outside a transaction, so the runner sets it before the migration's transaction begins.
    rebuild: bool = False


def _recorded(db) -> set[str]:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone():
        return set()
    return {row[0] for row in db.execute("SELECT id FROM schema_migrations")}


def refuse_unknown(db, migrations: Sequence[Migration]) -> None:
    """Raise UnknownMigration when the database records an id not in `migrations`."""
    unknown = sorted(_recorded(db) - {step.id for step in migrations})
    if unknown:
        raise UnknownMigration(
            "This database was upgraded by a newer Pi (migration "
            + ", ".join(unknown)
            + "). Run that Pi version again, or restore the snapshot taken before the update."
        )


def apply(db, migrations: Sequence[Migration], version: str) -> list[str]:
    """Run every migration the database has not recorded, in order; the ids it ran.

    `db` must be in autocommit mode (no open transaction)."""
    if len({step.id for step in migrations}) != len(migrations):
        raise ValueError("Migration ids must be unique.")
    refuse_unknown(db, migrations)
    db.execute(LEDGER)
    ran = []
    for step in migrations:
        if step.id in _recorded(db):
            continue
        enforced = db.execute("PRAGMA foreign_keys").fetchone()[0]
        if step.rebuild:
            db.execute("PRAGMA foreign_keys=OFF")
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                # Checked again under the write lock: another process may have run it meanwhile.
                if step.id not in _recorded(db):
                    step.run(db)
                    if not db.in_transaction:
                        raise RuntimeError(f"Migration {step.id} ended its own transaction.")
                    db.execute(
                        "INSERT INTO schema_migrations VALUES (?,?,?)",
                        (step.id, time.time(), version),
                    )
                    ran.append(step.id)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        finally:
            db.execute(f"PRAGMA foreign_keys={'ON' if enforced else 'OFF'}")
    return ran


def last(db) -> str | None:
    """The id of the migration applied last, or None."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone():
        return None
    row = db.execute("SELECT id FROM schema_migrations ORDER BY rowid DESC LIMIT 1").fetchone()
    return row[0] if row else None
