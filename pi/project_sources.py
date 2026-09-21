"""Live project metadata from Pi-owned sources, with authoritative privacy."""

from . import session_settings


def resolve(store, reference):
    with store._connect() as db:
        kind = reference.get("kind")
        task = None
        if kind == "conversation":
            session_id = reference.get("sessionId")
        elif kind == "task":
            task = db.execute(
                "SELECT session_id,outcome,archived_at FROM tasks WHERE id=?",
                (reference.get("taskId"),),
            ).fetchone()
            if task is None:
                return None
            session_id = task["session_id"]
        else:
            # File provenance needs an attachment store; a filesystem path or
            # a client-provided session label cannot establish it.
            return None
        session = db.execute(
            "SELECT title,status FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        privacy = session_settings.source_privacy(db, session_id) if session else None
        if privacy is None:
            return None
        return {
            "originSessionId": session_id,
            "label": task["outcome"] if task else session["title"] or "Conversation",
            "archived": bool(task is not None and task["archived_at"] is not None)
            or session["status"] in {"closed", "forked"},
            "privacy": privacy,
        }
