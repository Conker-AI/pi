"""Live project metadata from Pi-owned sources, with authoritative privacy.

A source whose chat is forgotten or in the Trash resolves to nothing: the project shows it as
an unavailable reference without its title, and a chat that selected it is refused
(project_source_unavailable) before its words reach a model. The link itself is kept, so
restoring the chat brings the source back.
"""

from pi.content import attachments
from pi.conversation import settings as session_settings
from pi.conversation import visibility


def resolve(store, reference):
    with store._connect() as db:
        db.execute("BEGIN")
        return resolve_db(db, reference)


def resolve_db(db, reference):
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
    elif kind == "file":
        if visibility.hidden(db, [reference.get("sessionId")]):
            return None
        return attachments.project_source(
            db,
            reference.get("sessionId"),
            reference.get("fileId"),
            session_settings.source_privacy,
        )
    else:
        # File provenance needs an attachment store; a filesystem path or
        # a client-provided session label cannot establish it.
        return None
    if visibility.hidden(db, [session_id]):
        return None
    session = db.execute("SELECT title,status FROM sessions WHERE id=?", (session_id,)).fetchone()
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
