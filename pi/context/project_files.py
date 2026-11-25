"""Files the owner sends in a project chat join that project (`files.to_project`, on unless set off).

Called inside the transaction that writes the owner's message (`pi.turns.attachment_turns.bind`),
so a file joins only once it is really sent, not when it is uploaded and removed again. Each file
becomes a live reference, as linking it by hand would (`pi.context.projects.mutate`): no content
is copied and nothing is granted. Nothing joins from a private chat, a private file, an archived
project, or a project already holding its 1000 references; a file already linked stays as it is.
Any of these leaves the message as sent.
"""

from __future__ import annotations

import json
import time

from pi.context import project_sources
from pi.conversation import settings as session_settings
from pi.core import owner_settings as owner_settings

SETTING = "files.to_project"
MAX_LINKS = 1000  # pi.context.projects.mutate's limit


def join(db, session_id: str, attachment_ids: list[str]) -> list[str]:
    """Link the chat's sent files to its project; the ids that joined."""
    if not attachment_ids or owner_settings.value_in(db, SETTING) is False:
        return []
    settings = session_settings._load(db, session_id)["settings"]
    project_id = settings.get("projectId")
    if not project_id or any(settings.get("privacy", {}).values()):
        return []
    project = db.execute(
        "SELECT links,archived_at FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if project is None or project["archived_at"] is not None:
        return []
    links = json.loads(project["links"])
    joined = []
    for identity in attachment_ids:
        reference = {"kind": "file", "sessionId": session_id, "fileId": identity}
        if len(links) >= MAX_LINKS or any(link["reference"] == reference for link in links):
            continue
        source = project_sources.resolve_db(db, reference)
        if source is None or source["archived"] or any(source["privacy"].values()):
            continue
        links.append(
            {
                "reference": reference,
                "mode": "live-reference",
                "snapshot": {"originSessionId": source["originSessionId"], "linkedAt": time.time()},
            }
        )
        joined.append(identity)
    if joined:
        db.execute(
            "UPDATE projects SET links=?,revision=revision+1,updated_at=? WHERE id=?",
            (json.dumps(links), time.time(), project_id),
        )
    return joined
