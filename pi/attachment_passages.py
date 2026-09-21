"""Stable references into immutable extracted text; offsets count Unicode characters."""

import hashlib
import re

SIZE = 1200


def split(identity, text):
    result = []
    for start in range(0, len(text), SIZE):
        end = min(start + SIZE, len(text))
        excerpt = text[start:end]
        result.append(
            {
                "id": f"{identity}:p{len(result)}",
                "start": start,
                "end": end,
                "text": excerpt,
                "sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
            }
        )
    return result


def citations(db, turn_id, text, supplied):
    """Recognize only references to this turn's immutable input attachments."""
    from . import attachments, session_settings
    from .citations import normalize

    result = [item for item in normalize(supplied) if not item["id"].startswith("attachment_")]
    requested = set(re.findall(r"\[\[(attachment_[a-f0-9]{32}:p[0-9]{1,3})\]\]", text))
    if not requested:
        return result
    rows = db.execute(
        "SELECT ma.attachment_id,m.session_id FROM turn_messages tm "
        "JOIN messages m ON m.id=tm.message_id JOIN message_attachments ma ON ma.message_id=m.id "
        "WHERE tm.turn_id=? AND tm.purpose='input' ORDER BY ma.position",
        (turn_id,),
    ).fetchall()
    for row in rows:
        try:
            record, _view, raw = attachments._verified(
                db, row["session_id"], row["attachment_id"], session_settings.source_privacy
            )
            source, status, _reason = attachments._extract(raw, record["media_type"])
            if status != "extracted" or source != record["extracted_text"]:
                continue
        except attachments.AttachmentError:
            continue
        for index, passage in enumerate(split(row["attachment_id"], source)):
            if passage["id"] in requested and len(result) < 100:
                # Keep no copied source excerpt or filename: current privacy and
                # removal are enforced by the passage resolver when inspected.
                result.append({"id": passage["id"], "label": f"Attachment passage {index + 1}"})
    return result
