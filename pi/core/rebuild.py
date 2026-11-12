"""Widen a table's CHECK constraint on a database an older Pi created.

SQLite cannot alter a CHECK, and `CREATE TABLE IF NOT EXISTS` leaves an existing table as it was,
so a new allowed value (a new change request kind) would fail every insert after an upgrade. When
the stored definition lacks `marker`, the table is rebuilt the way SQLite documents: a new table
with the current definition, every row copied by column name, the old one dropped, the new one
renamed into its place, all in one transaction, with foreign keys unchecked for the copy only.
Nothing references these tables and they carry no triggers or views, so nothing else needs
rewriting; indexes come back from the module's own schema (`CREATE INDEX IF NOT EXISTS`). A
database that already has the marker is left alone.
"""

from __future__ import annotations

import re

_CREATE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \(")


def widen(db, table: str, create: str, marker: str) -> bool:
    """Rebuild `table` from `create` (its `CREATE TABLE IF NOT EXISTS` statement) unless its
    stored definition already contains `marker`. True if it was rebuilt."""
    found = _CREATE.match(create.strip())
    if found is None or found.group(1) != table:
        raise ValueError("Give the table's own CREATE TABLE IF NOT EXISTS statement.")
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None or marker in row[0]:
        return False
    staged = f"{table}_widened"
    # Rows are copied as they are: a row an older Pi wrote stays even if a reference it holds is
    # no longer checkable. The setting cannot change inside a transaction, so it is set first.
    enforced = db.execute("PRAGMA foreign_keys").fetchone()[0]
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(f"DROP TABLE IF EXISTS {staged}")
        db.execute(_CREATE.sub(f"CREATE TABLE {staged} (", create.strip(), count=1))
        old = [column[1] for column in db.execute(f"PRAGMA table_info({table})")]
        new = {column[1] for column in db.execute(f"PRAGMA table_info({staged})")}
        columns = ",".join(column for column in old if column in new)
        db.execute(f"INSERT INTO {staged} ({columns}) SELECT {columns} FROM {table}")
        db.execute(f"DROP TABLE {table}")
        db.execute(f"ALTER TABLE {staged} RENAME TO {table}")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute(f"PRAGMA foreign_keys={'ON' if enforced else 'OFF'}")
    return True
