"""The turn loop: assemble context, call a model, append the result.

Deliberately thin. The loop must behave identically whatever answers it, so
everything provider-specific lives behind the adapter and everything durable
lives in the store. What is left here is the part that must never vary.

Two rules are structural rather than stylistic:

**Append-only.** Nothing here rewrites an earlier message. Current models bind
reasoning blocks to the producing model and reject edited history, and a loop
that rewrites turns breaks against them in ways that surface late and
everywhere at once.

**Fork, never truncate.** When a conversation outgrows its window the session is
closed with a summary and a child opened seeded by it, pointing back at the
parent. Dropping the middle would silently lose what was said; rewriting it
would break the first rule. Forking keeps the lineage walkable, which is also
how MemoryGate treats evidence.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from pi.actions.toolgate import ToolGateClient
from pi.context import controls as context_controls
from pi.context import retrieval as context_retrieval
from pi.conversation import settings as session_settings
from pi.conversation import submissions
from pi.conversation.store import Store
from pi.memory import store as memory_store
from pi.memory.service import Memory
from pi.models import live_stream
from pi.models.providers import ProviderUnavailable
from pi.models.routing import Router, history_size
from pi.schedule import tasks
from pi.turns import control as turn_control
from pi.turns import thinking as background_thinking
from pi.turns import tool_drafting
from pi.turns.attempts import Attempts
from pi.turns.failures import ActedWithoutReply, TurnFailed

# A turn that would exceed this many characters of history triggers a fork.
# Characters, not tokens, on purpose: a tokeniser is provider-specific and this
# threshold only has to be roughly right, while being wrong about which
# tokeniser applies would be quietly wrong for every provider but one.
DEFAULT_FORK_THRESHOLD_CHARS = 24_000


class Loop(Attempts):
    def __init__(
        self,
        store: Store,
        router: Router,
        *,
        system_prompt: str = "",
        fork_threshold_chars: int = DEFAULT_FORK_THRESHOLD_CHARS,
        toolgate: ToolGateClient | None = None,
        max_tool_steps: int = 4,
        memory: Memory | None = None,
    ) -> None:
        self.store = store
        self.memory = memory or Memory(store)
        self.router = router
        self.system_prompt = system_prompt
        self.fork_threshold_chars = fork_threshold_chars
        self.toolgate = toolgate
        # Stores an approved memory word for word (MemoryGate statement route), when connected.
        self.remember: Callable[..., Any] | None = None
        # A ceiling on how many times one turn may act. Not a safety boundary -
        # ToolGate is that - but a model looping on a failing tool would
        # otherwise spend indefinitely without ever answering.
        self.max_tool_steps = max_tool_steps
        self.thinking = background_thinking.Thinking(self)
        # Drafts the tools Conker asks for, after the chat has answered (started by the app).
        self.drafts = tool_drafting.Drafts(self)

    # --- context ----------------------------------------------------------

    # --- resuming a parked turn -------------------------------------------

    # --- the turn ---------------------------------------------------------

    def run_turn(self, session_id: str, user_text: str, context: dict | None = None, **kwargs):
        """One turn; its answer can be previewed live by `request_id` while it runs."""
        with live_stream.open_reply(kwargs.get("request_id")):
            return self._run_turn(session_id, user_text, context, **kwargs)

    def _run_turn(
        self,
        session_id: str,
        user_text: str,
        context: dict | None = None,
        *,
        request_id: str | None = None,
        task_id: str | None = None,
        task_expected_revision: int | None = None,
        draft_revision: int | None = None,
        attachment_ids: list[str] | None = None,
        queued_entry=None,
        model_id=None,
        reply_to=None,
        research_mode="off",
        skill_ids=None,
        think=False,
    ) -> dict:
        """One turn. Returns the assistant message and where it landed.

        The session id may change: if history has outgrown the window the turn
        forks first and runs in the child. Callers are told which session
        answered rather than left to assume it was the one they asked.
        """
        if len(user_text) > memory_store.MAX_CONTENT_CHARACTERS:
            raise TurnFailed("Send at most 16000 characters per message; split longer text.")
        explicit = request_id is not None
        if not explicit:
            session = self.store.get_session(session_id)
            if session is None:
                raise TurnFailed(f"no such session: {session_id}")
            if session["status"] != "open":
                raise TurnFailed(f"session {session_id} is {session['status']}, not open")
        identity = request_id or "legacy_" + uuid.uuid4().hex
        try:
            receipt, created = submissions.reserve(
                self.store,
                identity,
                session_id,
                user_text,
                context or {},
                task_id,
                task_expected_revision,
                **({"draft_revision": draft_revision} if draft_revision is not None else {}),
                **({"attachment_ids": attachment_ids} if attachment_ids else {}),
                **({"queued_entry": queued_entry} if queued_entry else {}),
                **({"model_id": model_id} if model_id is not None else {}),
                **({"reply_to": reply_to} if reply_to is not None else {}),
                **({"research_mode": research_mode} if research_mode != "off" else {}),
                **({"skill_ids": skill_ids} if skill_ids is not None else {}),
                think=think,
            )
        except tasks.TaskError as exc:
            if explicit:
                raise
            raise TurnFailed(str(exc)) from exc
        if not created:
            return {
                "session_id": receipt["effective_session_id"] or session_id,
                "turn_id": receipt["turn_id"],
                "status": receipt["status"],
                "acted": receipt["acted"],
                "replayed": True,
                "submission": receipt,
                "message": self.store.get_message(receipt["final_message_id"])
                if receipt["final_message_id"]
                else None,
            }
        try:
            execution = session_settings.execution(self.store, session_id, request_id=identity)
            from pi.context import research

            research.require_runtime(
                execution,
                self._available_tools(execution)
                if execution.get("researchMode") in ("web", "deep")
                else (),
                self.max_tool_steps,
            )
            turn_control.guard(self.store, execution)
            if (execution.get("configuration") or {}).get("modelId") and execution.get(
                "modelConfiguration"
            ) is None:
                raise TurnFailed(
                    "Explicit agent model mapping is not configured; no fallback was attempted."
                )
            adapters = self.router.adapters()
            context_retrieval.resolve(
                self.store, session_id, identity, adapters, turn_control.guard
            )
            history = self._history(session_id, execution=execution, request_id=identity)
            outgrown = history_size(history) + len(user_text) > self.fork_threshold_chars
            if outgrown and execution.get("replyToMessageId"):
                raise TurnFailed(
                    "Review a fork before replying; the selected target cannot be silently summarized."
                )
            if outgrown and execution.get("callExecution"):
                raise TurnFailed(
                    "Call context is full; end the call and review a conversation fork."
                )
            if outgrown and execution["privacy"]["harnessDisabled"]:
                raise TurnFailed(
                    "No harness excludes automatic summarization; use a reviewed fork."
                )
            if (
                outgrown
                and context_controls.load(self.store, session_id, request_id=identity)["policy"]
                is not None
            ):
                raise TurnFailed(
                    "Review context before forking; explicit pins and instructions remain intact."
                )
            if outgrown and task_id:
                raise submissions.SubmissionError(
                    "task_fork_required",
                    "This task needs its "
                    "original conversation. Create a child task before continuing after a fork.",
                )
            summary = self._summarise(history, execution) if outgrown else None
            receipt = submissions.bind(self.store, identity, fork_summary=summary)
        except Exception as exc:
            submissions.fail_preparation(
                self.store,
                identity,
                exc.detail["code"] if isinstance(exc, tasks.TaskError) else "preparation_failed",
            )
            stopped = submissions.get(self.store, identity)
            if stopped["status"] == "cancelled":
                return {
                    "session_id": session_id,
                    "turn_id": None,
                    "status": "cancelled",
                    "acted": False,
                    "message": None,
                    "submission": stopped,
                    "replayed": False,
                }
            if not explicit and isinstance(exc, tasks.TaskError):
                raise TurnFailed(str(exc)) from exc
            raise
        actual_session = receipt["effective_session_id"]
        try:
            result = self._run_bound(
                actual_session,
                user_text,
                receipt["turn_id"],
                context,
                session_id if actual_session != session_id else None,
            )
        except (context_controls.ContextError, ProviderUnavailable) as exc:
            acted = bool((self.store.get_turn(receipt["turn_id"]) or {}).get("acted"))
            self.store.finish_turn(
                receipt["turn_id"],
                "acted_no_reply" if acted else "failed",
                acted=int(acted),
                detail=str(exc),
            )
            if acted:
                raise ActedWithoutReply(receipt["turn_id"], str(exc)) from exc
            raise TurnFailed(str(exc), turn_id=receipt["turn_id"]) from exc
        if explicit:
            result["submission"] = submissions.get(self.store, identity)
            result["replayed"] = False
        return result
