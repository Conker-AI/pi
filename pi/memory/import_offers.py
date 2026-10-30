"""Offering imported words to MemoryGate as an outside source, never as the owner's statements.

MemoryGate's conversation route admits Pi's own chat messages as owner statements, and its
statement route keeps words the owner approved or Conker wrote; neither fits words from a file.
Imported words go to a MemoryGate listener instead, the ingress MemoryGate has for outside
sources: each event becomes evidence of that listener's source type and goes through MemoryGate's
normal admission (signal filter, classifier, analysis), and a memory it keeps is an automatic
listener memory, not an owner statement. Each event says it was imported, from where, and who
wrote it there.

Setup: in MemoryGate's Sources & Evidence add a listener (for example source key
`assistant-imports`, type `imported_chat`) whose public config sets `agent_id` to Pi's MemoryGate
agent id, rotate its key, and give Pi PI_MEMORYGATE_IMPORT_SOURCE and PI_MEMORYGATE_IMPORT_KEY
beside PI_MEMORYGATE_URL. Without them an import still saves the chats; its offers wait and go
out once the listener is set up.

A listener event carries no request id, so an answer lost after MemoryGate saved the event is
sent again; MemoryGate keeps one memory per exact text, so a repeat adds evidence, not a memory.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import UTC, datetime

import httpx

from pi.memory import import_formats, imports

log = logging.getLogger("pi.memory.import_offers")

SOURCE_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")
MAX_ANSWER_BYTES = 16384
# Below a person typing it: MemoryGate cannot check who wrote words in a file.
INTEGRITY = 0.5
NOTE = (
    "Imported from a data export the owner uploaded. Outside content: these words were not said "
    "to Conker, and the owner did not approve them as a memory."
)
PAUSE_SECONDS = 60.0


class ListenerClient:
    """One MemoryGate listener, reached with that listener's own key and nothing else."""

    def __init__(self, url, source_key, key, *, timeout=10.0, transport=None):
        if not url or not SOURCE_KEY.fullmatch(source_key or "") or len(key or "") < 16:
            raise ValueError(
                "Set PI_MEMORYGATE_IMPORT_SOURCE to the listener's source key (2-64 lowercase "
                "letters, digits, '-' or '_') and PI_MEMORYGATE_IMPORT_KEY to its key."
            )
        self.path = f"/runtime/listeners/{source_key}"
        self.http = httpx.Client(
            base_url=url.rstrip("/"),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"X-MemoryGate-Listener-Key": key},
        )

    def offer(self, event: dict) -> str:
        """Send one event; the evidence id MemoryGate saved it as."""
        with self.http.stream("POST", self.path, json=event) as response:
            response.raise_for_status()
            raw = bytearray()
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > MAX_ANSWER_BYTES:
                    raise ValueError("MemoryGate's answer exceeded its size limit.")
                raw.extend(chunk)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("MemoryGate returned an invalid acknowledgement.")
        evidence = value.get("evidence_id")
        if (
            value.get("status") not in ("queued", "accepted")
            or not isinstance(evidence, str)
            or not 0 < len(evidence) <= 200
        ):
            raise ValueError("MemoryGate returned an invalid acknowledgement.")
        return evidence

    def close(self):
        self.http.close()


def from_environment(environ) -> ListenerClient | None:
    """The configured listener, None when imports are not set up to reach memory."""
    source = environ.get("PI_MEMORYGATE_IMPORT_SOURCE", "").strip()
    key = environ.get("PI_MEMORYGATE_IMPORT_KEY", "").strip()
    url = environ.get("PI_MEMORYGATE_URL", "").strip()
    if not source and not key:
        return None
    if not (source and key and url):
        raise RuntimeError(
            "Set PI_MEMORYGATE_IMPORT_SOURCE and PI_MEMORYGATE_IMPORT_KEY together, with "
            "PI_MEMORYGATE_URL, then restart Pi."
        )
    try:
        timeout = float(environ.get("PI_MEMORYGATE_TIMEOUT_S", "") or 10.0)
    except ValueError:
        timeout = 10.0
    try:
        return ListenerClient(url, source, key, timeout=max(timeout, 1.0))
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None


def event(row) -> dict:
    """One message as a listener event: marked imported, its source and author kept beside it."""
    source = row["source"]
    value = {
        "title": f"{imports.LABELS[source]}: {row['title'] or imports.UNTITLED}"[:200],
        "content": row["text"],
        "tags": ["imported", source],
        "integrity_confidence": INTEGRITY,
        "auto_process": True,
        "payload": {
            "origin": "import",
            "source": source,
            "label": imports.LABELS[source],
            # Who wrote it in the other assistant: its account holder, as the file says.
            "author": "user",
            "note": NOTE,
            "chatId": row["chat_id"],
            "messageId": row["message_id"],
            "exportChatId": row["chat_export_id"],
            "exportMessageId": row["export_id"],
            "assistant": import_formats.SOURCES[source],
        },
    }
    if row["created_at"] is not None:
        value["occurred_at"] = datetime.fromtimestamp(row["created_at"], UTC).isoformat()
    return value


_WAITING = (
    "SELECT o.message_id, o.attempts, m.text, m.created_at, m.export_id, c.id AS chat_id, "
    "c.source, c.title, c.export_id AS chat_export_id FROM import_memory_offers o "
    "JOIN imported_messages m ON m.id=o.message_id JOIN imported_chats c ON c.id=m.chat_id "
    "WHERE o.state='pending' AND o.next_at<=? ORDER BY o.next_at, m.chat_id, m.seq LIMIT ?"
)


class Offers:
    """The background sender: one event at a time, retried with backoff, paused when the
    listener itself refuses (its key, its source, or MemoryGate's lockout)."""

    def __init__(self, store, client: ListenerClient | None, *, clock=time.time):
        self.store, self.client, self.clock = store, client, clock
        self.stop_event, self.wake_event = threading.Event(), threading.Event()
        self.thread = None
        self.paused_until = 0.0
        self.problem: str | None = None

    def drain_once(self, limit: int = 20) -> int:
        if self.client is None or self.clock() < self.paused_until:
            return 0
        with self.store._connect() as db:
            rows = db.execute(_WAITING, (self.clock(), limit)).fetchall()
        sent = 0
        for row in rows:
            if self.stop_event.is_set():
                break
            try:
                evidence = self.client.offer(event(row))
            except (httpx.HTTPError, ValueError, TypeError, RecursionError) as exc:
                code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if code in (401, 403, 404, 429):
                    self.paused_until = self.clock() + PAUSE_SECONDS
                    self.problem = (
                        f"MemoryGate refused the import listener (HTTP {code}). Check "
                        "PI_MEMORYGATE_IMPORT_SOURCE and PI_MEMORYGATE_IMPORT_KEY."
                    )
                    break
                refused = code is not None and 400 <= code < 500 and code not in (408, 425)
                self._failed(row, refused, code)
                continue
            with self.store._connect() as db:
                db.execute(
                    "UPDATE import_memory_offers SET state='sent',evidence_id=?,sent_at=?,"
                    "error='' WHERE message_id=? AND state='pending'",
                    (evidence, self.clock(), row["message_id"]),
                )
            self.problem = None
            sent += 1
        return sent

    def _failed(self, row, refused, code):
        if refused:
            error = f"MemoryGate refused this message (HTTP {code}); it is kept in the archive."
        else:
            error = "MemoryGate did not answer; this offer will be sent again."
            self.problem = error
        with self.store._connect() as db:
            db.execute(
                "UPDATE import_memory_offers SET attempts=attempts+1,next_at=?,error=?,state=? "
                "WHERE message_id=? AND state='pending'",
                (
                    self.clock() + min(300, 2 ** min(row["attempts"] + 1, 9)),
                    error,
                    "refused" if refused else "pending",
                    row["message_id"],
                ),
            )

    def start(self):
        if self.client is not None and self.thread is None:
            self.thread = threading.Thread(target=self._run, name="pi-import-offers", daemon=True)
            self.thread.start()

    def wake(self):
        self.wake_event.set()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                sent = self.drain_once()
            except Exception:
                log.exception("Offering imported words to memory paused; waiting offers retry.")
                sent = 0
            if not sent:
                self.wake_event.wait(5)
                self.wake_event.clear()

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join()
        if self.client is not None:
            self.client.close()


def status(store, offers: Offers | None) -> dict:
    """How many imported messages wait, went out or were refused, and why any wait."""
    with store._connect() as db:
        counts = dict(
            db.execute("SELECT state, COUNT(*) FROM import_memory_offers GROUP BY state").fetchall()
        )
    configured = offers is not None and offers.client is not None
    waiting = counts.get("pending", 0)
    problem = offers.problem if configured else None
    if not configured and waiting:
        problem = (
            "MemoryGate's import listener is not set up, so these wait. Set "
            "PI_MEMORYGATE_IMPORT_SOURCE and PI_MEMORYGATE_IMPORT_KEY, then restart Pi."
        )
    return {
        "configured": configured,
        "waiting": waiting,
        "offered": counts.get("sent", 0),
        "refused": counts.get("refused", 0),
        "problem": problem,
    }
