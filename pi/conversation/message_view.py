"""Messages as callers read them: forgotten tombstones, citations, attachments, versions and turn binding."""

from __future__ import annotations

import json
import sqlite3

from pi.content import attachments
from pi.conversation import citations as message_citations
from pi.conversation import settings as session_settings

MESSAGE_LOOKUP = """
SELECT m.*, r.id AS receipt_id, r.forgotten_at, c.body AS citation_body
FROM messages m
LEFT JOIN forgotten_sessions f ON f.session_id=m.session_id
LEFT JOIN forgetting_receipts r ON r.id=f.receipt_id
LEFT JOIN message_citations c ON c.message_id=m.id AND c.redacted=0
"""


def _message(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["content"] = json.loads(item["content"])
    evidence = item.pop("citation_body", None)
    if evidence and item["receipt_id"] is None:
        value = message_citations.normalize(json.loads(evidence))
        if value:
            item["citations"] = value
    if item["receipt_id"] is None:
        del item["receipt_id"], item["forgotten_at"]
    else:
        item["content_status"] = "forgotten"
    return item


def _message_with_attachments(db, row):
    item = _message(row)
    if item["role"] == "user":
        from pi.memory import delivery as memory_delivery

        item["memory_delivery"] = memory_delivery.for_message(db, item)
    if item.get("content_status") != "forgotten":
        version = db.execute(
            "SELECT v.root_message_id,v.retry_of,f.selected_message_id,f.revision "
            "FROM response_versions v JOIN response_families f "
            "ON f.root_message_id=v.root_message_id WHERE v.message_id=?",
            (item["id"],),
        ).fetchone()
        if version:
            item["response_family"] = dict(version)
        binding = db.execute(
            "SELECT tm.purpose,s.snapshot FROM turn_messages tm JOIN turn_settings s "
            "ON s.turn_id=tm.turn_id WHERE tm.message_id=?",
            (item["id"],),
        ).fetchone()
        if binding:
            snapshot = json.loads(binding["snapshot"])
            if item["role"] == "assistant" and snapshot.get("agentId"):
                item["agent_id"] = snapshot["agentId"]
            if binding["purpose"] == "input" and (target := snapshot.get("replyToMessageId")):
                item["reply_to"] = target
    files = attachments.message_views(db, item["id"], session_settings.source_privacy)
    if files:
        item["attachments"] = files
    return item
