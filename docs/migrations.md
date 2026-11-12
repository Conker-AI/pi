# Schema migrations

Pi keeps one SQLite database. Most of its setup is idempotent (`CREATE ... IF NOT EXISTS`) and runs
on every start. A **migration** is a step that changes what an older Pi left behind: a column it
lacked, a CHECK too narrow for a new value. Migrations are listed in order in
`pi/conversation/upgrades.py` and run by `pi/core/migrations.py`.

## The ledger

- Each migration has a fixed id (`0001-...`). It runs at most once per database, in one transaction
  together with its row in `schema_migrations` (`id`, `applied_at`, `pi_version`). A failure leaves
  neither the change nor the row, and the next start tries again.
- A migration that ends its own transaction is refused: the change and its record must land
  together.
- `GET /health` reports `schema_migration`: the id of the last migration this database recorded.

## Downgrades are refused

An id in `schema_migrations` that this Pi does not know was written by a newer Pi, whose schema
this Pi may misread. Pi then refuses to touch the database:

- the Store refuses to open (`UnknownMigration`), so Pi does not start;
- the startup forget queue refuses before it drops its restart hold or forgets a queued chat;
- the host forgetting command (`python -m pi.conversation.forgetting ... forget`) refuses before
  any write.

The message says what to do: run that newer Pi version again, or restore the snapshot taken before
the update. Rolling back Pi's code across a release that added a migration therefore needs the
pre-update snapshot of the database too.

## Adding a migration

Append it to `MIGRATIONS` (never rename, reorder or remove an id) and keep its body safe on a
database that already has the change, because a database from before the ledger may hold part of
it. A table rebuild sets `rebuild=True` so the runner turns foreign keys off around it.
