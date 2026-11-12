"""Widen a table's CHECK constraint on a database an older Pi created.

SQLite cannot alter a CHECK, and `CREATE TABLE IF NOT EXISTS` leaves an existing table as it was,
so a new allowed value (a new change request kind) would fail every insert after an upgrade. When
the stored definition lacks `marker`, the table is rebuilt the way SQLite documents: a new table
with the current definition, every row copied by column name, the old one dropped, the new one
renamed into its place, and the old table's indexes recreated. It runs as a migration
(`pi.core.migrations`, `rebuild=True`): inside the migration's transaction, with foreign keys
unchecked for the copy. Nothing references these tables and they carry no triggers or views, so
nothing else needs rewriting. A database that already has the marker is left alone.
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
    # Rows are copied as they are: a row an older Pi wrote stays even if a reference it holds is
    # no longer checkable. That setting cannot change inside a transaction, so the caller sets it.
    if not db.in_transaction or db.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Rebuild a table inside a transaction with foreign keys off.")
    indexes = [
        sql
        for (sql,) in db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,),
        )
    ]
    staged = f"{table}_widened"
    db.execute(f"DROP TABLE IF EXISTS {staged}")
    db.execute(_CREATE.sub(f"CREATE TABLE {staged} (", create.strip(), count=1))
    old = [column[1] for column in db.execute(f"PRAGMA table_info({table})")]
    new = {column[1] for column in db.execute(f"PRAGMA table_info({staged})")}
    columns = ",".join(column for column in old if column in new)
    db.execute(f"INSERT INTO {staged} ({columns}) SELECT {columns} FROM {table}")
    db.execute(f"DROP TABLE {table}")
    db.execute(f"ALTER TABLE {staged} RENAME TO {table}")
    for sql in indexes:
        db.execute(sql)
    return True
