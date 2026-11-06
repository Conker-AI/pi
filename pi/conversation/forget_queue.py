"""Chats the owner chose to delete forever, queued for Pi's next start.

Forgetting needs every Pi process stopped (pi.conversation.forgetting holds an exclusive lease),
so the owner route forgets nothing itself. It checks the owner's confirmation against a fresh
preview, writes the request to a file beside the database and asks the supervisor to restart Pi.
On the next start, before the Store opens, `run_pending` runs the offline command for each chat
in a child process, exactly as an operator would. A chat whose sessions changed since the owner
confirmed is refused, never widened. The request and result files hold ids, counts and states
only, never a title or a word of a chat.

This module never imports the forgetting code: it only runs the command.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

from pi.core.errors import ServiceError

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PREVIEW_SECONDS = 60
FORGET_SECONDS = 3600
KEEP_RESULTS = 50
DETAIL_CHARS = 300
CHANGED = "The chat changed since you confirmed it, so nothing was deleted. Review it again."
_restart_once = threading.Lock()


class ForgetRefused(ServiceError):
    pass


def supervised() -> bool:
    """Whether something restarts Pi when it exits (Compose sets PI_SUPERVISED)."""
    return os.environ.get("PI_SUPERVISED", "").strip().lower() in {"1", "true", "yes"}


def _request_path(db: Path | str) -> Path:
    return Path(f"{db}.forget-request.json")


def _result_path(db: Path | str) -> Path:
    return Path(f"{db}.forget-result.json")


def _read(path: Path) -> list:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def _write(path: Path, items: list) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(items, file, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _command(db: Path | str, *args: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pi.conversation.forgetting", "--db", str(db), *args],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def preview_many(db: Path | str, sessions: list[str]) -> dict:
    """Each session's plan, or {"error": message} for one that cannot be forgotten."""
    args = [part for session in sessions for part in ("--session", session)]
    try:
        done = _command(db, "preview-many", *args, timeout=PREVIEW_SECONDS)
    except subprocess.TimeoutExpired:
        done = None
    if done is None or done.returncode != 0:
        detail = (done.stderr.strip() if done else "The preview took too long.")[:DETAIL_CHARS]
        raise ForgetRefused("forgetting_unavailable", detail or "The preview failed.", 503)
    return json.loads(done.stdout)


def summary(session: str, plan: dict) -> dict:
    """Counts and ids for the owner to confirm: never a message id list or any text."""
    prior = plan.get("already_forgotten")
    if prior:
        sessions, messages, turns = (
            prior["session_ids"],
            prior["message_count"],
            prior["turn_count"],
        )
        approvals = 0
    else:
        sessions, messages = plan["session_ids"], len(plan["message_ids"])
        turns, approvals = len(plan["turn_ids"]), len(plan["approval_request_ids"])
    return {
        "sessionId": session,
        "sessionIds": sorted(sessions),
        "messages": messages,
        "turns": turns,
        "approvalRequests": approvals,
        "confirmation": plan["confirmation"],
        "alreadyForgotten": bool(prior),
    }


def overview(db: Path | str) -> dict:
    return {
        "supervised": supervised(),
        "pending": [item["sessionId"] for item in _read(_request_path(db))],
        "results": _read(_result_path(db)),
    }


def queue(db: Path | str, items: list[dict]) -> None:
    """Add {sessionId, sessionIds} items; a chat already queued keeps one entry."""
    path = _request_path(db)
    merged = {item["sessionId"]: item for item in _read(path)}
    merged.update({item["sessionId"]: item for item in items})
    _write(path, list(merged.values()))


def _record(db: Path | str, session: str, state: str, receipt: str | None, detail: str) -> None:
    path = _result_path(db)
    kept = [item for item in _read(path) if item["sessionId"] != session]
    kept.append(
        {
            "sessionId": session,
            "state": state,
            "receiptId": receipt,
            "finishedAt": time.time(),
            "detail": detail[:DETAIL_CHARS] or None,
        }
    )
    _write(path, kept[-KEEP_RESULTS:])


def _unfinished(db: Path | str) -> bool:
    try:
        with closing(sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro", uri=True)) as conn:
            return bool(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='forgetting_maintenance'"
                ).fetchone()
                and conn.execute("SELECT 1 FROM forgetting_maintenance").fetchone()
            )
    except sqlite3.Error:
        return False


def _forget(db: Path | str, item: dict) -> tuple[str, str | None, str]:
    """Run one queued chat; answers (state, receipt id, detail)."""
    session = item["sessionId"]
    try:
        plan = preview_many(db, [session])[session]
    except ForgetRefused as exc:
        return "retrying", None, exc.message
    if "error" in plan:
        return "refused", None, plan["error"]
    if not plan.get("already_forgotten") and set(plan["session_ids"]) != set(item["sessionIds"]):
        return "refused", None, CHANGED
    try:
        done = _command(
            db, "forget", "--session", session, "--confirm", plan["confirmation"],
            timeout=FORGET_SECONDS,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return "retrying", None, "Forgetting took too long."
    if done.returncode != 0:
        return "retrying", None, done.stderr.strip()
    return "done", json.loads(done.stdout)["id"], ""


def run_pending(db: Path | str) -> None:
    """Forget every queued chat before the Store opens; call once at startup.

    A chat that cannot be forgotten is recorded as refused and dropped from the queue. One whose
    cleanup started stays queued (state retrying): the Store then refuses to start, the
    supervisor restarts Pi, and the next start finishes it.
    """
    path = _request_path(db)
    for item in _read(path):
        state, receipt, detail = _forget(db, item)
        if state == "retrying" and _unfinished(db):
            _record(db, item["sessionId"], state, None, detail)
            return
        _record(db, item["sessionId"], "refused" if state == "retrying" else state, receipt, detail)
        _write(path, [left for left in _read(path) if left["sessionId"] != item["sessionId"]])


def restart_soon(delay: float = 1.0, grace: float = 30.0) -> None:
    """Stop this process so the supervisor starts it again and runs the queue.

    The reply that queued the chats is sent first. Open event streams can hold a graceful
    shutdown, so the process exits on its own after `grace` seconds.
    """
    if not _restart_once.acquire(blocking=False):
        return

    def stop() -> None:
        # A daemon watchdog: it dies with a clean shutdown and ends a stuck one.
        watchdog = threading.Timer(grace, os._exit, (0,))
        watchdog.daemon = True
        watchdog.start()
        os.kill(os.getpid(), signal.SIGTERM)

    timer = threading.Timer(delay, stop)
    timer.daemon = True
    timer.start()
