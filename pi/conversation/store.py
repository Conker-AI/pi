"""Session state and execution history. Pi holds nothing else.

Append-only is enforced here rather than promised in a docstring: there is no
runtime function that updates or deletes a message, and the schema forbids it
with triggers. The separate offline owner command can redact content atomically,
leaving identifiers and a receipt; forgotten sessions never resume. Models bind
reasoning blocks to the producing model and reject
edited history, and by the time that surfaces the offending code is everywhere.

Transcripts live here and only here. What crosses into MemoryGate is derived
evidence, never these rows - see ADR-0002. Message ids are therefore stable and
never reused: evidence cites them, and a citation that can be re-pointed is not
a citation.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pi.actions import identities as actions
from pi.agents import profiles as agents
from pi.calls import call_turns
from pi.context import research_usage
from pi.conversation import citations as message_citations
from pi.conversation import schema, submissions
from pi.conversation import settings as session_settings
from pi.conversation import versions as response_versions
from pi.conversation.access import MaintenanceRequired, acquire
from pi.conversation.message_view import MESSAGE_LOOKUP, _message_with_attachments
from pi.conversation.schema import FORGETTING_SCHEMA, SCHEMA
from pi.turns import control as turn_control
from pi.turns import restart as turn_restart
from pi.turns import steering as turn_steering


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lease = acquire(self.path)
        self._release = weakref.finalize(self, lease.close)
        try:
            with self._connect() as db:
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='recovery_deletion_hold'"
                ).fetchone():
                    raise MaintenanceRequired(
                        "Recovered data is held pending coordinated deletion, effect and authority reconciliation."
                    )
                if (
                    db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='forgetting_maintenance'"
                    ).fetchone()
                    and db.execute("SELECT 1 FROM forgetting_maintenance").fetchone()
                ):
                    raise MaintenanceRequired(
                        "Forgetting cleanup is unfinished. Stop Pi and rerun the same "
                        "forgetting command before starting it."
                    )
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA foreign_keys=ON")
                db.executescript(SCHEMA)
                self._migrate(db)
                db.executescript(FORGETTING_SCHEMA)
                schema.install(db)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Release the lifetime lease only after all turns have stopped."""
        self._release()

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if not self._release.alive:
            raise RuntimeError("Store is closed; create a new Store before using it")
        db = sqlite3.connect(self.path, isolation_level=None, timeout=10.0)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            db.close()

    # --- sessions ---------------------------------------------------------

    def create_session(
        self,
        title: str = "",
        parent_id: str | None = None,
        summary: str | None = None,
        agent_id: str | None = None,
        privacy: dict[str, bool] | None = None,
    ) -> str:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session_id = self._create_session(db, title, parent_id, summary, agent_id, privacy)
            db.commit()
            return session_id

    def _create_session(
        self, db, title="", parent_id=None, summary=None, agent_id=None, privacy=None
    ) -> str:
        """Insert a session inside the caller's transaction, without inference."""
        if privacy is not None and (
            not set(privacy) <= {"memoryDisabled", "harnessDisabled"}
            or any(type(value) is not bool for value in privacy.values())
        ):
            raise ValueError("Use only boolean memoryDisabled and harnessDisabled restrictions.")
        session_id = f"ses_{uuid.uuid4().hex[:16]}"
        initial_settings = None
        if parent_id is None:
            initial_settings = json.loads(json.dumps(session_settings.DEFAULT))
            choice = db.execute(
                "SELECT choice FROM setup_optional_choices WHERE step='memory' "
                "ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            if choice is not None:
                initial_settings["privacy"]["memoryDisabled"] = choice[0] == "skip"
            if privacy:
                initial_settings["privacy"].update(privacy)
            if agent_id is not None:
                selected = agents._get(db, agent_id)
                if selected["archived_at"] is not None:
                    raise agents.AgentError("agent_archived", "Select an active agent.")
                initial_settings["agentId"] = selected["id"]
            elif choice is None and not privacy:
                initial_settings = None
        db.execute(
            "INSERT INTO sessions (id, parent_id, title, status, created_at, summary)"
            " VALUES (?,?,?,'open',?,?)",
            (session_id, parent_id, title, time.time(), summary),
        )
        if parent_id is not None:
            session_settings.copy_parent_settings(db, parent_id, session_id)
        if initial_settings is not None:
            db.execute(
                "INSERT INTO session_settings VALUES (?,?,?)",
                (session_id, 1, json.dumps(initial_settings)),
            )
        return session_id

    def get_session(self, session_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close_session(self, session_id: str, status: str, summary: str | None = None) -> None:
        """Close a session. The messages stay exactly as they were."""
        if status not in {"forked", "closed"}:
            raise ValueError(f"a session closes as forked or closed, not {status!r}")
        with self._connect() as db:
            db.execute(
                "UPDATE sessions SET status=?, closed_at=?, summary=COALESCE(?, summary)"
                " WHERE id=?",
                (status, time.time(), summary, session_id),
            )

    # --- messages ---------------------------------------------------------

    def append_message(
        self,
        session_id: str,
        role: str,
        content: Any,
        *,
        turn_id=None,
        purpose=None,
        action_id=None,
    ) -> dict:
        """Add one message to the end of a session. There is no other way in.

        No update, no delete, no reorder - not because callers are trusted but
        because the functions do not exist and the triggers would refuse them.
        """
        with self._connect() as db:
            # Sequence allocation and the outbox trigger share the message commit.
            db.execute("BEGIN IMMEDIATE")
            message = submissions.append(
                db, session_id, role, content, turn_id=turn_id, purpose=purpose, action_id=action_id
            )
            db.commit()
        return message

    def messages(self, session_id: str) -> list[dict]:
        with self._connect() as db:
            db.execute("BEGIN")
            rows = db.execute(
                MESSAGE_LOOKUP + " WHERE m.session_id=? ORDER BY m.seq", (session_id,)
            ).fetchall()
            return [_message_with_attachments(db, row) for row in rows]

    def get_message(self, message_id: str) -> dict | None:
        """Resolve an evidence citation, including its content-free tombstone."""
        with self._connect() as db:
            db.execute("BEGIN")
            row = db.execute(MESSAGE_LOOKUP + " WHERE m.id=?", (message_id,)).fetchone()
            return _message_with_attachments(db, row) if row else None

    # --- turns ------------------------------------------------------------

    def start_turn(self, session_id: str) -> str:
        turn_id = f"trn_{uuid.uuid4().hex[:16]}"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO turns (id, session_id, status, started_at) VALUES (?,?, 'running', ?)",
                (turn_id, session_id, time.time()),
            )
            session_settings.bind(db, turn_id, session_id)
            db.commit()
        return turn_id

    def finish_turn(
        self, turn_id: str, status: str, *, expected_status: str = "running", **fields: Any
    ) -> bool:
        with self._connect() as db:
            return self._finish_turn(db, turn_id, status, expected_status=expected_status, **fields)

    @staticmethod
    def _finish_turn(db, turn_id, status, *, expected_status="running", **fields):
        fields = research_usage.account(db, turn_id, fields)
        allowed = {
            "provider",
            "model",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "cost_usd",
            "latency_ms",
            "detail",
            "route_tier",
            "route_reason",
            "approval_request_id",
            "approval_tool_id",
            "approval_args",
            "approval_expires_at",
            "approval_intent",
            "acted",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown turn fields: {sorted(unknown)}")
        if (
            status == "failed"
            and db.execute(
                "SELECT 1 FROM turn_cancellations WHERE turn_id=?", (turn_id,)
            ).fetchone()
        ):
            status = "cancelled"
        sets = ", ".join(f"{k}=?" for k in fields)
        clause = f", {sets}" if sets else ""
        return (
            db.execute(
                f"UPDATE turns SET status=?, ended_at=?{clause} WHERE id=? AND status=?",
                (status, time.time(), *fields.values(), turn_id, expected_status),
            ).rowcount
            == 1
        )

    def complete_turn(self, turn_id, text, *, citations=None, reply_request_id=None, **fields):
        """A final reply and terminal state are one commit, including exact message provenance."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT session_id,status FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                raise RuntimeError("Turn is no longer claimed by this caller")
            snapshot = db.execute(
                "SELECT snapshot FROM turn_settings WHERE turn_id=?", (turn_id,)
            ).fetchone()
            execution = json.loads(snapshot[0]) if snapshot else None
            call_turns.guard_db(db, execution)
            turn_control.guard_db(db, turn_id, reply_request_id)
            turn_steering.guard_db(db, turn_id)
            message = submissions.append(
                db, row["session_id"], "assistant", text, turn_id=turn_id, purpose="final"
            )
            if execution and execution.get("agentId"):
                message["agent_id"] = execution["agentId"]
            from pi.content import attachment_passages

            citations = attachment_passages.citations(db, turn_id, text, citations)
            evidence = message_citations.save(db, message["id"], citations)
            if evidence:
                message["citations"] = evidence
            fields = turn_steering.account(db, turn_id, fields)
            if not self._finish_turn(db, turn_id, "complete", **fields):
                raise RuntimeError("Turn is no longer claimed by this caller")
            turn_restart.release(db, turn_id)
            retry = db.execute(
                "SELECT source_message_id FROM response_retries WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if retry:
                root = response_versions.register(db, retry[0], message["id"])
                response_versions.activate(db, row["session_id"], root, message["id"])
            db.commit()
            return message

    def claim_turn(self, turn_id: str, expected_status: str) -> bool:
        with self._connect() as db:
            return (
                db.execute(
                    "UPDATE turns SET status='running',ended_at=NULL "
                    "WHERE id=? AND status=? AND NOT EXISTS "
                    "(SELECT 1 FROM turns other WHERE other.session_id=turns.session_id "
                    "AND other.id!=turns.id AND other.status='running') AND NOT EXISTS "
                    "(SELECT 1 FROM turn_submissions s WHERE s.requested_session_id="
                    "turns.session_id AND s.state='preparing')",
                    (turn_id, expected_status),
                ).rowcount
                == 1
            )

    def acted_without_reply(self) -> list[dict]:
        """Every turn that changed the world but never said what happened.

        The same reasoning as the approval queue: an action whose result the
        owner never sees is, to them, indistinguishable from one that silently
        went wrong. These are all resumable, and resuming asks only for the
        missing reply - the action is not repeated.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM turns WHERE (status='acted_no_reply' "
                "OR (status='interrupted' AND acted=1))"
                " AND session_id NOT IN (SELECT session_id FROM forgotten_sessions)"
                " ORDER BY started_at"
            ).fetchall()
        return [
            {**dict(r), "approval_args": json.loads(r["approval_args"] or "null")} for r in rows
        ]

    def mark_acted(self, turn_id: str) -> None:
        """Record that this turn has now changed the world, before anything else.

        Called the moment a tool returns, and deliberately not folded into
        `finish_turn`: the turn is still running, so `ended_at` must stay unset.
        The ordering is the point. Between the action happening and the model
        narrating it there is a window where the process can die, and a record
        written only afterwards would leave the owner reading `interrupted` for
        an action that already ran.
        """
        with self._connect() as db:
            db.execute("UPDATE turns SET acted=1 WHERE id=?", (turn_id,))

    def awaiting_approval(self) -> list[dict]:
        """Every turn parked on the owner, across all sessions.

        The dashboard needs one queue rather than a per-session hunt: an
        approval the owner never sees is an action that silently never happens.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM turns WHERE status='awaiting_approval'"
                " AND session_id NOT IN (SELECT session_id FROM forgotten_sessions)"
                " ORDER BY started_at"
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["approval_args"] = json.loads(item["approval_args"] or "null")
            out.append(item)
        return out

    def get_turn(self, turn_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["approval_args"] = json.loads(item["approval_args"] or "null")
        latest = actions.latest(self, turn_id)
        item["action"] = {key: latest[key] for key in ("id", "state", "job_id")} if latest else None
        return item

    def turns(self, session_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM turns WHERE session_id=? ORDER BY started_at", (session_id,)
            ).fetchall()
            action_rows = db.execute(
                "SELECT a.turn_id,a.id,a.state,a.job_id FROM tool_actions a "
                "JOIN turns t ON t.id=a.turn_id WHERE t.session_id=? "
                "AND a.rowid=(SELECT MAX(rowid) FROM tool_actions WHERE turn_id=a.turn_id)",
                (session_id,),
            ).fetchall()
        latest = {
            row["turn_id"]: {key: row[key] for key in ("id", "state", "job_id")}
            for row in action_rows
        }
        return [
            {
                **dict(row),
                "approval_args": json.loads(row["approval_args"] or "null"),
                "action": latest.get(row["id"]),
            }
            for row in rows
        ]

    def mark_interrupted_turns(self) -> int:
        """Called at startup. A turn that was running when the process died did
        not finish, and saying nothing about it is the one answer that is wrong -
        the owner would see a request that simply vanished.

        What Pi can still settle from its own records goes first, without running
        anything again: an answer already produced is saved, and Conker's own
        tool calls cut off mid-way are recorded as filed or not (`turns.restart`).
        """
        turn_restart.recover(self)
        turn_restart.settle_own_tools(self)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            preparing = submissions.recover_preparations(db)
            cursor = db.execute(
                "UPDATE turns SET status=CASE WHEN EXISTS(SELECT 1 FROM tool_actions a "
                "WHERE a.turn_id=turns.id "
                "AND a.state IN ('dispatching','action_in_progress','outcome_unknown')) "
                "THEN 'outcome_unknown' WHEN acted=1 THEN 'acted_no_reply' "
                "WHEN EXISTS(SELECT 1 FROM turn_cancellations c WHERE c.turn_id=turns.id) "
                "THEN 'cancelled' "
                "ELSE 'interrupted' END, ended_at=?,"
                # An interrupted turn that had already acted is not the same event
                # as one that had not, and that difference is what the owner needs
                # from this row: in plain words, in the order of the status above,
                # with what they can do now.
                " detail=CASE WHEN EXISTS(SELECT 1 FROM tool_actions a WHERE a.turn_id=turns.id"
                " AND a.state IN ('dispatching','action_in_progress','outcome_unknown'))"
                "   THEN 'Pi restarted while a tool action was running. Check its outcome "
                "before sending this again; it will not run twice.'"
                "   WHEN acted=1 THEN 'Pi restarted after the action ran, before the reply. "
                "Ask for the reply; the action will not run again.'"
                "   WHEN EXISTS(SELECT 1 FROM turn_cancellations c WHERE c.turn_id=turns.id)"
                "   THEN 'Pi restarted while this turn was stopping. Your message is kept.'"
                "   ELSE 'Pi restarted before the answer was finished. Your message is kept; "
                "send it again to retry.' END"
                # Only 'running'. A turn parked on the owner is not interrupted -
                # it is waiting, and a restart does not withdraw the question.
                " WHERE status='running'"
                " AND session_id NOT IN (SELECT session_id FROM forgotten_sessions)",
                (time.time(),),
            )
            count = cursor.rowcount + preparing
            db.commit()
            return count

    def health(self) -> dict:
        try:
            with self._connect() as db:
                db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone()
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "unavailable", "reason": type(exc).__name__}
