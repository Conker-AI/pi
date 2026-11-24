"""One turn's attempt: guard, context, the model and tool loop, and finishing. Split from loop.py (a move)."""

from __future__ import annotations

import json
import time
from typing import Any, cast

from pi.actions import tools as tool_protocol
from pi.actions.toolgate import (
    ApprovalRequired,
    ToolGateClient,
    ToolGateUnavailable,
    ToolPending,
    ToolRefused,
    ToolResult,
)
from pi.conversation import settings as session_settings
from pi.models.providers import ProviderUnavailable, shown_text
from pi.models.routing import TurnContext, history_size
from pi.turns import access, native_tools, tool_drafting
from pi.turns import action_identities as actions
from pi.turns import control as turn_control
from pi.turns import steering as turn_steering
from pi.turns import thinking as background_thinking
from pi.turns.failures import ActedWithoutReply, TurnFailed
from pi.turns.history import History
from pi.turns.model_calls import ModelCalls, without_effort
from pi.turns.replies import Replies


class Attempts(ModelCalls, History, Replies):
    """Part of Loop: runs one bound turn attempt. Uses the attributes Loop.__init__ sets.

    Derives from the other parts so the type checker sees the methods it calls on self; Loop
    derives from this class alone, and its method order is unchanged.
    """

    # Set by Loop.__init__ (pi/turns/loop.py); declared here for the type checker only.
    thinking: background_thinking.Thinking

    def _run_bound(self, session_id, user_text, turn_id, context, forked_from, continued=False):
        from pi.models import routing_memo

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
        # His answer to the drafter's question waiting in this chat goes to that draft instead.
        if not continued and (
            answered := tool_drafting.answered(self, session_id, turn_id, user_text, execution)
        ):
            return {**answered, "forked_from": forked_from}
        if not continued:
            self.memory.prepare(turn_id, user_text)
        # Held for the whole turn: if this parks on an approval, the owner needs
        # to see what they asked for next to what it produced.
        intent = user_text
        started = time.monotonic() if started is None else started

        from pi.context import research
        from pi.turns import research_plan

        published = self._available_tools(execution)
        # Conker's own tools only file requests for the owner's approval (see native_tools).
        # They follow the owner's tool choice: offered when ToolGate is set up and not skipped,
        # and never what this turn may not use (a private chat, an AI job's run).
        own = (
            native_tools.offered(self, execution, session_id, turn_id)
            if self._offers_tools()
            else []
        )
        available = research.tools(execution, published + own)
        allowed = {t.id for t in available}
        catalog = {t.id: t for t in available}
        delegation = []

        def delegate():
            # Asked once per turn, only when a ToolGate tool is about to run.
            if not delegation:
                check = getattr(self.toolgate, "delegate", None)
                delegation.append(bool(check and check()))
            return delegation[0]

        acted = bool(cast("dict", self.store.get_turn(turn_id))["acted"])
        context = dict(context or {})
        # Only published tools need a tool-capable model; Conker's own requests do not.
        context.setdefault("needs_tools", bool(published))
        history = self._history(session_id, tools=available, turn_id=turn_id)
        ctx = TurnContext(history_chars=history_size(history), **context)

        try:
            from pi.models import inference_control

            local_keys = background_thinking.foreground_local_keys(self, execution)
            with inference_control.foreground_intent(local_keys):
                if not continued:
                    self.thinking.start(session_id, turn_id, history, ctx, execution)
                if (
                    execution.get("researchMode") == "deep"
                    and research.plan(self.store, turn_id) is None
                ):
                    _, planned, _ = self._call(
                        [*history, research.PLAN_PROMPT], ctx, without_effort(execution)
                    )
                    research_plan.save_plan(self.store, turn_id, planned.text, self.max_tool_steps)
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
                # Deep research saved its plan above (save_plan raises otherwise).
                limit = min(
                    limit,
                    cast("dict[str, Any]", research.plan(self.store, turn_id))["search_limit"],
                )
            for step in range(max(0, limit - used)):
                route, completion, call = self._requested(
                    route, completion, history, ctx, execution, allowed, available
                )
                if call is None:
                    break
                if web_research:
                    research.validate_call(call, self.store, turn_id)
                ran = False
                try:
                    # prepare returns the action row it has just inserted.
                    action = cast(
                        "dict[str, Any]",
                        actions.prepare(
                            self.store,
                            turn_id,
                            call.tool_id,
                            call.args,
                            ToolGateClient.new_action_id(),
                            proposal=completion.text,
                        ),
                    )
                except turn_steering.Pending:
                    turn_steering.discarded(self.store, turn_id, completion)
                    raise
                try:
                    turn_control.guard(self.store, execution)
                    tool = catalog.get(call.tool_id)
                    native = native_tools.owns(call.tool_id)
                    # His access layers decide run or ask; ToolGate enforces the floor again.
                    choice, decided_by, why = (
                        access.decide(
                            self,
                            tool,
                            call.args,
                            execution=execution,
                            session_id=session_id,
                            turn_id=turn_id,
                            native=native_tools.owns,
                            delegate=delegate,
                        )
                        if tool is not None
                        else (None, "tool_default", "")
                    )
                    if native:
                        outcome = native_tools.call(
                            self,
                            call.tool_id,
                            call.args,
                            session_id=session_id,
                            turn_id=turn_id,
                            execution=execution,
                            run=choice == "run",
                        )
                    else:
                        decision: dict[str, Any] = (
                            {
                                "decision": {
                                    "outcome": choice,
                                    "decided_by": decided_by,
                                    "reason": why,
                                }
                            }
                            if choice
                            else {}
                        )
                        outcome = self._gate(execution).invoke(
                            call.tool_id,
                            call.args,
                            action_id=action["id"],
                            job_id=action["job_id"],
                            **decision,
                        )
                    ran_alone = (
                        choice == "run"
                        and isinstance(outcome, ToolResult)
                        and (not native or outcome.result.get("status") == "done")
                        and access.loosened(tool, decided_by, native)
                    )
                    if ran_alone:
                        access.record(
                            self.store,
                            session_id=session_id,
                            turn_id=turn_id,
                            action_id=action["id"],
                            tool_id=call.tool_id,
                            decided_by=decided_by,
                            reason=why,
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
                    ran = outcome.changed
                    observation = json.dumps(
                        {"tool": call.tool_id, "result": outcome.result}, ensure_ascii=False
                    )

                # The action row prepared above exists, so latest is never None here.
                if (
                    cast("dict[str, Any]", actions.latest(self.store, turn_id))["state"]
                    != "completed"
                ):
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
            shown_text(completion),
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
