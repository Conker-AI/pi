"""His answers from the Inbox continue the work instead of waiting for him to open the chat.

The gateway calls `decided` after ToolGate has recorded his decision on a request, from the
browser's Inbox or the `conker` CLI. Pi grants nothing here: Do it replays the exact stored action
and ToolGate checks his approval as it is spent. A "no" only ever closes: the action never runs,
the chat records that he said no, and the chat takes messages again. Words he wrote with it
(Change) become his next message in that chat, so whatever Conker does next passes every access
layer again. An answer to one of Conker's questions arrives the same way (`deliver`).
"""

from __future__ import annotations

import json
import logging
import threading
import time

from pi.actions import identities as actions
from pi.agents import asks
from pi.conversation import submissions
from pi.schedule import tasks

log = logging.getLogger(__name__)

DECLINED = "The owner said no to this action in his Inbox. It did not run."
CLOSED = "You said no in the Inbox. Nothing was done."
CHANGED = "You said what to do instead in the Inbox. This action was not done."
BUSY_WAIT, BUSY_TRIES = 5.0, 60


def background(work) -> None:
    """Run after the answer is recorded; the Inbox does not wait for the model."""

    def run():
        try:
            work()
        except Exception:
            log.exception("Continuing after the owner's answer failed; the chat shows the state.")

    threading.Thread(target=run, name="pi-owner-answer", daemon=True).start()


def waiting(store, request_id):
    """("turn", row) for a chat or AI-job turn parked on this request, ("run", id) for a tool
    cron job's run, or None when nothing waits on it any more."""
    with store._connect() as db:
        turn = db.execute(
            "SELECT id, session_id, approval_tool_id FROM turns "
            "WHERE status='awaiting_approval' AND approval_request_id=?",
            (request_id,),
        ).fetchone()
        if turn is not None:
            return "turn", dict(turn)
        for run in db.execute(
            "SELECT id, receipt FROM scheduled_runs WHERE status='awaiting_approval'"
        ).fetchall():
            try:
                if json.loads(run["receipt"] or "{}").get("request_id") == request_id:
                    return "run", run["id"]
            except ValueError:
                continue
    return None


def close(store, turn, note="") -> bool:
    """End a parked turn he said no to. False when someone else (the chat, a second answer)
    already took it."""
    if not store.claim_turn(turn["id"], "awaiting_approval"):
        return False
    action = actions.latest(store, turn["id"])
    if action is not None:
        actions.state(store, action["id"], "refused")
    # Pi's own note beside the tool's id, never inside a result: tool output cannot forge it.
    store.append_message(
        turn["session_id"],
        "tool",
        json.dumps({"tool": turn["approval_tool_id"], "declined": DECLINED}, ensure_ascii=False),
        turn_id=turn["id"],
        purpose="tool_result",
        action_id=action["id"] if action else None,
    )
    store.finish_turn(turn["id"], "cancelled", detail=CHANGED if note else CLOSED)
    return True


def _busy(store, session_id) -> bool:
    with store._connect() as db:
        return submissions._busy(db, session_id)


def deliver(loop, session_id, text, base_id, *, ask_id=None, sleep=time.sleep):
    """Send his words as his next message in that chat, once it is free. Each attempt has its
    own submission id (a refused one is permanent); an ask records which one carries it."""
    for attempt in range(BUSY_TRIES):
        if _busy(loop.store, session_id):
            sleep(BUSY_WAIT)
            continue
        request_id = f"{base_id}-{attempt}"
        if ask_id:
            asks.delivering(loop.store, ask_id, request_id)
        try:
            return loop.run_turn(session_id, text, request_id=request_id)
        except tasks.TaskError as exc:
            if exc.detail.get("code") != "session_busy":
                raise
        sleep(BUSY_WAIT)
    log.warning("The chat stayed busy; the owner's answer was not delivered.")
    return None


def decided(loop, request_id, status, note="", *, resume_run=None, cancel_run=None) -> dict:
    """Continue whatever waits on this request after his decision in ToolGate."""
    found = waiting(loop.store, request_id)
    if found is None:
        return {"continued": "nothing_waiting"}
    kind, target = found
    if kind == "run":
        if status == "approved":
            if resume_run is None:
                return {"continued": "nothing_waiting"}
            background(lambda: resume_run(target))
            return {"continued": "running", "runId": target}
        if cancel_run is not None:
            cancel_run(target)
        return {"continued": "closed", "runId": target}
    receipt = {"turnId": target["id"], "sessionId": target["session_id"]}
    if status == "approved":
        background(lambda: loop.resume_turn(target["id"]))
        return {"continued": "running", **receipt}
    words = (note or "").strip()
    if not close(loop.store, target, words):
        return {"continued": "nothing_waiting"}
    if words:
        background(lambda: deliver(loop, target["session_id"], words, f"change-{target['id']}"))
        return {"continued": "your_words", **receipt}
    return {"continued": "closed", **receipt}


def answered(loop, ask) -> None:
    """Deliver his answer to one of Conker's questions as his message in that chat."""
    text = f"About “{ask['question']}”: {ask['answer']}"
    background(
        lambda: deliver(loop, ask["sessionId"], text, f"answer-{ask['id']}", ask_id=ask["id"])
    )
