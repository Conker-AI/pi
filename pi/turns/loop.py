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

import json
import time
import uuid

from pi.actions import identities as actions
from pi.actions import tools as tool_protocol
from pi.actions.toolgate import (
    ApprovalRequired,
    ToolGateClient,
    ToolGateUnavailable,
    ToolPending,
    ToolRefused,
)
from pi.context import controls as context_controls
from pi.context import retrieval as context_retrieval
from pi.conversation import live_stream, submissions
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.memory import store as memory_store
from pi.memory.service import Memory
from pi.models.providers import ProviderUnavailable
from pi.models.routing import Router, TurnContext, history_size
from pi.schedule import tasks
from pi.turns import control as turn_control
from pi.turns import steering as turn_steering
from pi.turns import thinking as background_thinking
from pi.turns.failures import ActedWithoutReply, TurnFailed
from pi.turns.history import History
from pi.turns.model_calls import ModelCalls
from pi.turns.replies import Replies

# A turn that would exceed this many characters of history triggers a fork.
# Characters, not tokens, on purpose: a tokeniser is provider-specific and this
# threshold only has to be roughly right, while being wrong about which
# tokeniser applies would be quietly wrong for every provider but one.
DEFAULT_FORK_THRESHOLD_CHARS = 24_000


class Loop(ModelCalls, History, Replies):
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
        # A ceiling on how many times one turn may act. Not a safety boundary -
        # ToolGate is that - but a model looping on a failing tool would
        # otherwise spend indefinitely without ever answering.
        self.max_tool_steps = max_tool_steps
        self.thinking = background_thinking.Thinking(self)

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
            context_retrieval.resolve(self.store, session_id, identity, adapters)
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

    def _run_bound(self, session_id, user_text, turn_id, context, forked_from, continued=False):
        from pi.turns import routing_memo

        turn_steering.active(self.store, turn_id, True)
        started = time.monotonic()
        try:
            while True:
                try:
                    with routing_memo.scope(self.store.path, turn_id):
                        return self._run_bound_attempt(
                            session_id, user_text, turn_id, context, forked_from, continued, started
                        )
                except turn_steering.Pending:
                    continued = True
        finally:
            turn_steering.active(self.store, turn_id, False)

    def _run_bound_attempt(
        self, session_id, user_text, turn_id, context, forked_from, continued=False, started=None
    ):
        execution = session_settings.execution(self.store, session_id, turn_id)
        turn_control.guard(self.store, execution)
        if not continued:
            self.memory.prepare(turn_id, user_text)
        # Held for the whole turn: if this parks on an approval, the owner needs
        # to see what they asked for next to what it produced.
        intent = user_text
        started = time.monotonic() if started is None else started

        from pi.context import research

        available = research.tools(execution, self._available_tools(execution))
        allowed = {t.id for t in available}
        acted = bool(self.store.get_turn(turn_id)["acted"])
        context = dict(context or {})
        context.setdefault("needs_tools", bool(available))
        history = self._history(session_id, tools=available, turn_id=turn_id)
        ctx = TurnContext(history_chars=history_size(history), **context)

        try:
            from pi.turns import inference_control

            local_keys = background_thinking.foreground_local_keys(self, execution)
            with inference_control.foreground_intent(local_keys):
                if not continued:
                    self.thinking.start(session_id, turn_id, history, ctx, execution)
                if (
                    execution.get("researchMode") == "deep"
                    and research.plan(self.store, turn_id) is None
                ):
                    _, planned, _ = self._call([*history, research.PLAN_PROMPT], ctx, execution)
                    research.save_plan(self.store, turn_id, planned.text, self.max_tool_steps)
                    history = self._history(session_id, tools=available, turn_id=turn_id)
                route, completion, skipped = self._call(history, ctx, execution)

            web_research = execution.get("researchMode") in ("web", "deep")
            if web_research and not research.action_count(self.store, turn_id):
                research.validate_call(tool_protocol.parse(completion.text, allowed))

            # Act, then think again, up to a ceiling. Every action goes through
            # ToolGate; Pi runs nothing itself.
            with self.store._connect() as db:
                used = db.execute(
                    "SELECT COUNT(*) FROM tool_actions WHERE turn_id=?", (turn_id,)
                ).fetchone()[0]
            limit = self.max_tool_steps
            if execution.get("researchMode") == "web":
                limit = 1
            elif execution.get("researchMode") == "deep":
                limit = min(limit, research.plan(self.store, turn_id)["search_limit"])
            for step in range(max(0, limit - used)):
                call = tool_protocol.parse(completion.text, allowed)
                if call is None:
                    break
                if web_research:
                    research.validate_call(call, self.store, turn_id)
                ran = False
                try:
                    action = actions.prepare(
                        self.store,
                        turn_id,
                        call.tool_id,
                        call.args,
                        ToolGateClient.new_action_id(),
                        proposal=completion.text,
                    )
                except turn_steering.Pending:
                    turn_steering.discarded(self.store, turn_id, completion)
                    raise
                try:
                    turn_control.guard(self.store, execution)
                    outcome = self.toolgate.invoke(
                        call.tool_id, call.args, action_id=action["id"], job_id=action["job_id"]
                    )
                except ToolRefused as refusal:
                    if refusal.code == "BUDGET_DENIED":
                        return self._hold(
                            turn_id,
                            ToolPending(
                                "awaiting_budget",
                                "Owner spending configuration required: " + refusal.message,
                                action["id"],
                            ),
                        )
                    actions.state(self.store, action["id"], "refused")
                    # A refusal is an answer. It goes back to the model as an
                    # observation, never retried with the guard removed.
                    observation = f"tool {call.tool_id} refused: {refusal.code} - {refusal.message}"
                except ToolGateUnavailable as exc:
                    return self._hold(
                        turn_id, ToolPending("outcome_unknown", exc.reason, action["id"])
                    )
                else:
                    if isinstance(outcome, ToolPending):
                        return self._hold(turn_id, outcome)
                    if isinstance(outcome, ApprovalRequired):
                        actions.state(self.store, action["id"], "awaiting_approval")
                        # Park. The owner has not said no - they have not been
                        # asked yet, and a failed turn would say the wrong thing.
                        self.store.finish_turn(
                            turn_id,
                            "awaiting_approval",
                            provider=completion.provider,
                            model=completion.model,
                            route_tier=route.tier.value,
                            route_reason=route.reason.value,
                            latency_ms=int((time.monotonic() - started) * 1000),
                            approval_request_id=outcome.request_id,
                            approval_tool_id=outcome.tool_id,
                            approval_args=json.dumps(outcome.args, ensure_ascii=False),
                            approval_expires_at=outcome.expires_at,
                            approval_intent=intent,
                            detail=outcome.message,
                        )
                        return {
                            "session_id": session_id,
                            "forked_from": forked_from,
                            "turn_id": turn_id,
                            "status": "awaiting_approval",
                            "approval": {
                                "request_id": outcome.request_id,
                                "action_id": action["id"],
                                "tool_id": outcome.tool_id,
                                "args": outcome.args,
                                "expires_at": outcome.expires_at,
                                "asked": intent,
                                "message": outcome.message,
                            },
                            "message": None,
                            "memory": self.memory.status(session_id, turn_id),
                        }
                    actions.record(self.store, action, outcome)
                    ran = outcome.ok
                    observation = json.dumps(
                        {"tool": call.tool_id, "result": outcome.result}, ensure_ascii=False
                    )

                if actions.latest(self.store, turn_id)["state"] != "completed":
                    self.store.append_message(
                        session_id,
                        "tool",
                        observation,
                        turn_id=turn_id,
                        purpose="tool_result",
                        action_id=action["id"],
                    )
                if ran:
                    # Written before the next model call, which can fail.
                    self.store.mark_acted(turn_id)
                    acted = True
                history = self._history(
                    session_id,
                    tools=[] if used + step + 1 >= limit else available,
                    turn_id=turn_id,
                    reply_only=used + step + 1 >= limit,
                )
                route, completion, skipped = self._call(history, ctx, execution)
            research.validate_narration(execution, completion.text)
            tool_protocol.validate_answer(completion.text)
            turn_control.guard(self.store, execution)
        except (ProviderUnavailable, RuntimeError) as exc:
            # The user's message stays. It was said, and a transcript that drops
            # what was said because the answer failed is not a transcript.
            reason = getattr(exc, "reason", type(exc).__name__)
            latency = int((time.monotonic() - started) * 1000)
            if acted:
                # A tool already ran in this turn. Whatever then happened to the
                # model, the world changed, and "failed" would deny it.
                self.store.finish_turn(
                    turn_id, "acted_no_reply", acted=1, detail=reason, latency_ms=latency
                )
                raise ActedWithoutReply(turn_id, reason) from exc
            self.store.finish_turn(turn_id, "failed", detail=reason, latency_ms=latency)
            raise TurnFailed(reason, turn_id=turn_id) from exc

        message = self._complete_turn(
            turn_id,
            completion.text,
            citations=completion.citations,
            provider=completion.provider,
            model=completion.model,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cached_tokens=completion.cached_tokens,
            cost_usd=completion.cost_usd,
            latency_ms=int((time.monotonic() - started) * 1000),
            # Recorded so the policy can be tuned against outcomes rather than
            # opinion: a cheap model that fails and escalates has cost both.
            route_tier=route.tier.value,
            route_reason=route.reason.value,
            acted=int(acted),
            # Which candidates refused, so a model that always refuses is
            # visible in the record rather than only as latency.
            detail=route.detail(skipped),
        )
        return {
            "session_id": session_id,
            "forked_from": forked_from,
            "turn_id": turn_id,
            "acted": acted,
            "route": route.receipt(skipped),
            "message": message,
            "memory": self.memory.status(session_id, turn_id),
        }
