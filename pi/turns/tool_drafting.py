"""Pi's drafting worker: Conker's tool requests are drafted here, in the background, one at a time.

Filing a tool request (`pi.agents.tool_drafts`) wakes the worker; the chat that asked has already
answered. It drafts from the tools Conker can use, as ToolGate lists them for Conker's key
(published workflows included), with the `drafting` model role, or the answer role until the
owner picks a drafting model. Each model call gets its role's own time limit; on a shared local
model it gives way to a chat reply and then goes on; stopping Pi cancels the call that is running.
ToolGate's document check (`ToolGateClient.check_document`) reads every draft; a ToolGate that
does not answer, or an older one without the check, leaves the draft unchecked. A draft that was
running when Pi stopped is marked failed when Pi starts again (`tool_drafts.recover_interrupted`).

`announce` is Conker's message in the chat that asked, once the owner has published the tool: a
note Pi stores, like its other notes, never a model's words.

When the drafter needs a detail for a request from a chat, `ask` posts its question there (a
stored message, the question in the drafter's words) and the card waits (`needs_detail`). The
owner's next message in that chat is the answer (`answered`, called by the turn loop before any
model call): drafting goes on with the questions and answers so far, and Conker's stored reply
says so. His answer from the Inbox (`retry` with `answer`) goes on the same way.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import unicodedata

from pi.actions.toolgate import ToolGateUnavailable
from pi.agents import asks, draft_capabilities, tool_drafts
from pi.conversation import submissions
from pi.turns import answers, inference_control, native_tools

log = logging.getLogger(__name__)

YIELDS = 3  # times one model call gives way to a chat reply before the draft stops
LIVE = "“{name}” is live. Want me to run it now?"
QUESTION = (
    "One question before I can draft your tool: {question}\nReply here and I'll go on drafting."
)
GOING_ON = "Thanks. I'm drafting the tool with that; it will be in your Inbox."
BUSY_WAIT, BUSY_TRIES = 5.0, 60
NO_TOOLS = "Tools are not set up, so there is nothing to build a tool from."
NO_TOOLGATE = "ToolGate didn't answer, so Conker's tools could not be listed. Try again."
REASONS = {
    "deadline": (
        "The drafting model ran past its time limit. Try again, or give the drafting role more "
        "time or a faster model in Settings > Models."
    ),
    "preempted": "Chat replies kept the model busy, so drafting stopped. Try again.",
    "shutdown": tool_drafts.RESTARTED,
}
BROKEN = "Drafting stopped on an unexpected error. Try again."


class Unbuildable(Exception):
    """Nothing to draft from; the message is the reason the owner sees."""


class Stopped(Exception):
    """A model call was stopped: past its time limit, by chat replies or by Pi stopping."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Drafts:
    """The worker. `start` runs it in a thread; `kick` wakes it; `tick` drafts what waits."""

    def __init__(self, loop, *, interval: float = 60.0):
        self.loop = loop
        self.interval = interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._token = None
        self._thread = None

    def start(self):
        if self._thread is not None:
            raise RuntimeError("The drafting worker was already started.")
        self._thread = threading.Thread(target=self._run, name="pi-tool-drafts", daemon=True)
        self._thread.start()

    def kick(self):
        self._wake.set()

    def close(self):
        self._stop.set()
        self._wake.set()
        with self._lock:
            token = self._token
        if token is not None:
            token.cancel("shutdown")
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.tick()
            except Exception:
                # Model text may hold the owner's words; the request's card keeps its state.
                log.error("A drafting pass failed; the requests keep their state.")
            self._wake.wait(self.interval)

    def tick(self) -> int:
        """Draft every request that waits, oldest first; how many were drafted."""
        done = 0
        for request in tool_drafts.pending(self.loop.store):
            if self._stop.is_set():
                break
            self.draft(request)
            done += 1
        return done

    def draft(self, request) -> None:
        store, description = self.loop.store, request["payload"]["description"]
        said = tool_drafts.conversation(request)
        title = summary = None
        try:
            payload, title, summary = tool_drafts.build(
                store,
                self.loop.router.adapters(),
                description,
                self._capabilities(),
                check=self._check,
                attempt=self._attempt,
                alive=lambda: (
                    not self._stop.is_set() and tool_drafts.still_drafting(store, request)
                ),
                conversation=said,
                # A request from a chat asks there; one without a chat fails with the question.
                ask=request["sessionId"] is not None,
            )
        except Unbuildable as reason:
            payload = tool_drafts.failed(description, str(reason), said)
        except Stopped as stopped:
            payload = tool_drafts.failed(description, REASONS.get(stopped.reason, BROKEN), said)
        except Exception:
            log.error("A tool draft failed on an unexpected error.")
            payload = tool_drafts.failed(description, BROKEN, said)
        # The store may close while Pi stops; then Pi marks the draft failed when it starts.
        with contextlib.suppress(RuntimeError):
            if (
                tool_drafts.finish(store, request, payload, title, summary)
                and payload["status"] == tool_drafts.NEEDS_DETAIL
            ):
                ask(store, {**request, "payload": payload})

    def _capabilities(self) -> list[dict]:
        if not self.loop._offers_tools():
            raise Unbuildable(NO_TOOLS)
        try:
            tools = self.loop._gate(None).tools()
        except ToolGateUnavailable:
            raise Unbuildable(NO_TOOLGATE) from None
        return draft_capabilities.available(tools)

    def _check(self, document):
        checker = getattr(self.loop.toolgate, "check_document", None)
        if checker is None:
            return None
        try:
            return checker(document)
        except ToolGateUnavailable:
            return None

    def _attempt(self, seconds, call):
        """One model call within its time limit, cancellable, giving way to chat replies."""
        for _ in range(YIELDS + 1):
            token = inference_control.Token()
            with self._lock:
                self._token = token
                if self._stop.is_set():
                    token.cancel("shutdown")
            try:
                with inference_control.bind(token, background=True), token.deadline(seconds):
                    return call()
            except inference_control.InferenceCancelled:
                if token.reason != "preempted" or self._stop.is_set():
                    raise Stopped(
                        "shutdown" if self._stop.is_set() else token.reason or "deadline"
                    ) from None
            finally:
                with self._lock:
                    if self._token is token:
                        self._token = None
        raise Stopped("preempted")


_BUSY = object()


def _plain(name: str) -> str:
    """The tool's name on one line, without control characters."""
    kept = "".join(c for c in name if unicodedata.category(c)[0] != "C" or c in "\t\n")
    return " ".join(kept.split())[:160] or "Your tool"


def _open_chat(db, session_id):
    """The chat itself, or the one it continues in after forks; None when it is gone."""
    for _ in range(50):
        row = db.execute("SELECT id, status FROM sessions WHERE id=?", (session_id,)).fetchone()
        if (
            row is None
            or db.execute(
                "SELECT 1 FROM forgotten_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        ):
            return None
        if row["status"] == "open":
            return row["id"]
        if row["status"] != "forked":
            return None
        child = db.execute(
            "SELECT id FROM sessions WHERE parent_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if child is None:
            return None
        session_id = child["id"]
    return None


def _post(store, session_id, text, claim=None):
    """Conker's stored message in the chat, or the one it continues in; None when there is no
    such chat or `claim(db, chat)` (run in the same transaction) says not to post."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        target = _open_chat(db, session_id)
        if target is None:
            return None
        # A reply in progress, or a message being prepared, would see the chat change under it.
        if (
            submissions._busy(db, target)
            or db.execute(
                "SELECT 1 FROM turn_submissions WHERE requested_session_id=? AND state='preparing'",
                (target,),
            ).fetchone()
        ):
            return _BUSY
        if claim is not None and not claim(db, target):
            return None
        message = submissions.append(db, target, "assistant", text)
        db.commit()
    return message


def _deliver(store, session_id, text, sleep, missed, claim=None):
    """Post now, or in the background once the chat is free; the message, or None."""
    posted = _post(store, session_id, text, claim)
    if posted is not _BUSY:
        return posted

    def later():
        for _ in range(BUSY_TRIES):
            sleep(BUSY_WAIT)
            if _post(store, session_id, text, claim) is not _BUSY:
                return
        log.warning(missed)

    answers.background(later)
    return None


def announce(store, session_id, name, *, sleep=time.sleep):
    """Conker says in the chat that asked that the tool is live: a stored message, no model call.
    While the chat is busy it waits in the background; the message, or None."""
    text = LIVE.format(name=_plain(name))
    missed = "The chat stayed busy; the note that the tool is live was not posted."
    return _deliver(store, session_id, text, sleep, missed)


def ask(store, request, *, sleep=time.sleep):
    """Conker asks the drafter's question in the chat that asked, as a stored message. The card
    records the chat (`askedIn`) in the same commit, so the owner's next message there answers
    it; an answer from the Inbox first means nothing is posted. Waits while the chat is busy."""
    text = QUESTION.format(question=" ".join(request["payload"]["question"].split()))

    def claim(db, chat):
        return tool_drafts.mark_asked(db, request, chat)

    missed = "The chat stayed busy; the drafter's question was not posted. It waits in the Inbox."
    return _deliver(store, request["sessionId"], text, sleep, missed, claim)


def ask_waiting(store) -> int:
    """At start: questions the drafter asked that Pi stopped before posting go to their chats."""
    waiting = [
        request
        for request in tool_drafts.asking(store)
        if request["sessionId"] and not request["payload"].get("askedIn")
    ]
    for request in waiting:
        ask(store, request)
    return len(waiting)


def answered(loop, session_id, turn_id, text, execution) -> dict | None:
    """The owner's message answers the drafter's question waiting in this chat: drafting goes on
    with it and Conker says so, a stored reply instead of a model call. None when no question
    waits here, so the turn goes on as usual. A call, an AI job's run and an answer to one of
    Conker's own questions (`conker.ask`) are never taken as the answer."""
    store, started = loop.store, time.monotonic()
    asked = [item for item in tool_drafts.asking(store) if item["payload"].get("askedIn")]
    if not asked or execution.get("callExecution") or native_tools.job_run(store, turn_id):
        return None
    with store._connect() as db:
        if asks.answer_words(db, turn_id) is not None:
            return None
        waiting = [
            item for item in asked if _open_chat(db, item["payload"]["askedIn"]) == session_id
        ]
    for request in waiting:
        if tool_drafts.take_answer(store, request, text):
            break
    else:
        return None
    drafts = getattr(loop, "drafts", None)
    if drafts is not None:
        drafts.kick()
    message = loop._complete_turn(
        turn_id, GOING_ON, latency_ms=int((time.monotonic() - started) * 1000)
    )
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "acted": False,
        "message": message,
        "memory": loop.memory.status(session_id, turn_id),
    }
