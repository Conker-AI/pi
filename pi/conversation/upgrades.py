"""Pi's schema migrations, in the order they run; `pi.core.migrations` runs and records them.

They run after every table's idempotent setup (`schema.install`). Append new ones at the end and
never rename, reorder or remove an id: a database records the ids it ran, and a Pi that finds one
it does not know refuses to start (a downgrade).

The first five were guarded steps Pi ran on every start before the ledger existed, moved here
unchanged; each still checks before it changes, since a database from before the ledger may
already hold part of it. Steps left outside the ledger, and why: research_usage.migrate and
memory.store.migrate add columns that the per-start step right after them needs; memory.store's
schema rebuilds its queue trigger and backfills on every start by design; continuity.initialize
keeps its own record (`continuity_migrations`).
"""

from __future__ import annotations

from pi.agents import change_requests, keep
from pi.content import attachments
from pi.core.migrations import Migration
from pi.owner import notices
from pi.setup import model_probes


def turn_columns(db):
    """Add columns a newer Pi expects to a database an older one created.

    CREATE TABLE IF NOT EXISTS silently does nothing when the table already
    exists, so a new column would be missing on every install that ever ran
    an earlier version - and the failure would appear as a write error long
    after the upgrade.
    """
    have = {row["name"] for row in db.execute("PRAGMA table_info(turns)")}
    for column in (
        "route_tier",
        "route_reason",
        "approval_request_id",
        "approval_tool_id",
        "approval_args",
        "approval_expires_at",
        "approval_intent",
    ):
        if column not in have:
            db.execute(f"ALTER TABLE turns ADD COLUMN {column} TEXT")
    if "acted" not in have:
        db.execute("ALTER TABLE turns ADD COLUMN acted INTEGER NOT NULL DEFAULT 0")
    if "usage_version" not in have:
        # Existing turns remain NULL: their unrecorded earlier calls cannot
        # be reconstructed from a final completion's counters.
        db.execute("ALTER TABLE turns ADD COLUMN usage_version INTEGER")
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS new_turn_usage_version AFTER INSERT ON turns "
        "BEGIN UPDATE turns SET usage_version=1 WHERE id=NEW.id; END"
    )


MIGRATIONS = (
    Migration("0001-turn-route-approval-usage", turn_columns),
    Migration("0002-setup-probe-operation", model_probes.add_operation),
    Migration("0003-attachment-storage", attachments.add_storage),
    Migration("0004-change-request-agent-team", change_requests.widen_kinds, rebuild=True),
    Migration("0005-keep-result-agent-team", keep.widen_kinds, rebuild=True),
    # Plan alerts and AI job notes into one notices table (ids kept); the two old tables go.
    Migration("0006-notices", notices.merge_old_tables),
)
