"""Bounded read projections of Pi's actual turns and content-free state events."""
from . import submissions
from .tasks import TaskError


def _available(db, session_id):
    return db.execute("SELECT 1 FROM sessions WHERE id=? AND status!='forgotten' "
                      "AND id NOT IN (SELECT session_id FROM forgotten_sessions)",
                      (session_id,)).fetchone() is not None


def _run(db, row):
    # No guessed message ownership: existing messages have no turn_id association.
    value = {key: row[key] for key in ("id", "session_id", "status", "provider", "model",
                                      "started_at", "ended_at")}
    value.update(acted=bool(row["acted"]), provenance="recorded", outputs=[],
                 source={"kind": "conversation", "session_id": row["session_id"]},
                 content_status="available" if _available(db, row["session_id"])
                 else "forgotten")
    action = db.execute("SELECT id,state,job_id FROM tool_actions WHERE turn_id=? "
                        "ORDER BY rowid DESC LIMIT 1", (row["id"],)).fetchone()
    value["action"] = dict(action) if action else None
    value["task_ids"] = [item[0] for item in db.execute(
        "SELECT task_id FROM task_runs WHERE run_id=? ORDER BY task_id", (row["id"],)
    )]
    value["message_refs"] = submissions.message_refs(db, row["id"])
    return value


def get_run(store, run_id):
    with store._connect() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM turns WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise TaskError("not_found", "Run not found.", 404)
        return _run(db, row)


def list_runs(store, limit=50, cursor=None, session_id=None, task_id=None):
    limit = min(max(limit, 1), 200)
    with store._connect() as db:
        db.execute("BEGIN")
        where, values = [], []
        if cursor:
            before = db.execute("SELECT started_at FROM turns WHERE id=?", (cursor,)).fetchone()
            if not before:
                raise TaskError("invalid_cursor", "The run cursor is unavailable.", 422)
            where.append("(started_at,id) < (?,?)")
            values.extend((before[0], cursor))
        if session_id:
            where.append("session_id=?")
            values.append(session_id)
        if task_id:
            where.append("id IN (SELECT run_id FROM task_runs WHERE task_id=?)")
            values.append(task_id)
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = db.execute("SELECT * FROM turns" + clause +
                          " ORDER BY started_at DESC,id DESC LIMIT ?",
                          (*values, limit + 1)).fetchall()
        page = rows[:limit]
        return {"results": [_run(db, row) for row in page],
                "next_cursor": page[-1]["id"] if len(rows) > limit else None}


def list_events(store, limit=50, cursor=None, session_id=None, task_id=None, run_id=None):
    limit = min(max(limit, 1), 200)
    with store._connect() as db:
        db.execute("BEGIN")
        where, values = [], []
        if cursor:
            if not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 18:
                raise TaskError("invalid_cursor", "Use the event cursor returned by this API.", 422)
            where.append("sequence < ?")
            values.append(int(cursor))
        if task_id:
            # Current explicit associations, not a claim that a task owned every past run event.
            where.append("(task_id=? OR (task_id IS NULL AND run_id IN "
                         "(SELECT run_id FROM task_runs WHERE task_id=?)))")
            values.extend((task_id, task_id))
        for field, value in (("session_id", session_id), ("run_id", run_id)):
            if value:
                where.append(field + "=?")
                values.append(value)
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = db.execute("SELECT * FROM activity_events" + clause +
                          " ORDER BY sequence DESC LIMIT ?", (*values, limit + 1)).fetchall()
        page = rows[:limit]
        return {"results": [{**dict(row), "content_status": "available" if
                             _available(db, row["session_id"]) else "forgotten"} for row in page],
                "next_cursor": str(page[-1]["sequence"]) if len(rows) > limit else None}
