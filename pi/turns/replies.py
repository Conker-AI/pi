"""Finishing turns: resuming an approved action, recovering a lost reply, completing or holding a turn."""

from __future__ import annotations

import json
import time

from pi.actions import identities as actions
from pi.actions import tools as tool_protocol
from pi.actions.toolgate import ApprovalRequired, ToolGateUnavailable, ToolPending, ToolRefused
from pi.calls import call_turns
from pi.conversation import settings as session_settings
from pi.models.providers import ProviderUnavailable
from pi.models.routing import TurnContext, history_size
from pi.turns import control as turn_control
from pi.turns import steering as turn_steering
from pi.turns.failures import ActedWithoutReply, TurnFailed


class Replies:
    """Part of Loop: replies and resumption. Uses the attributes Loop.__init__ sets."""

    def resume_turn(self, turn_id: str, job_id: str | None = None) -> dict:
        """Continue a turn the owner has now approved.

        The stored action is replayed exactly as it was shown to them - same
        tool, same arguments. Rebuilding it from the conversation instead would
        risk spending an approval on a different action than the one approved,
        which is the failure the whole binding exists to prevent.

        A turn that already acted but never got its reply also resumes here, and
        skips straight to the reply. Its approval is spent, so re-invoking would
        fail closed at best and run the action twice at worst.
        """
        turn = self.store.get_turn(turn_id)
        if turn is None:
            raise TurnFailed(f"no such turn: {turn_id}")
        recoverable = {
            "awaiting_approval",
            "awaiting_budget",
            "acted_no_reply",
            "action_in_progress",
            "outcome_unknown",
        }
        if turn["acted"] and turn["status"] == "interrupted":
            recoverable.add("interrupted")
        if turn["status"] not in recoverable:
            raise TurnFailed(f"turn {turn_id} is {turn['status']}, not awaiting approval")

        session_id = turn["session_id"]
        session = self.store.get_session(session_id)
        if session and session["status"] == "forgotten":
            raise TurnFailed("session is forgotten; this turn cannot resume")
        execution = session_settings.execution(self.store, session_id, turn_id)
        # A stopped action may still need read-only receipt reconciliation.
        # Dispatch and provider checkpoints below retain the stop guard.
        call_turns.guard(self.store, execution)
        if not self.store.claim_turn(turn_id, turn["status"]):
            raise TurnFailed("Another caller already resumed this turn", turn_id)
        started = time.monotonic()
        acted = bool(turn["acted"])
        action = actions.latest(self.store, turn_id)

        if turn["status"] in {
            "awaiting_approval",
            "awaiting_budget",
            "action_in_progress",
            "outcome_unknown",
        }:
            if self.toolgate is None:
                self.store.finish_turn(turn_id, turn["status"])
                raise TurnFailed("no action boundary is configured")
            if action is None:
                return self._hold(
                    turn_id,
                    ToolPending(
                        "outcome_unknown",
                        "Legacy action ID missing; reconcile before any new dispatch",
                        "",
                    ),
                )
            try:
                if turn["status"] in {"awaiting_approval", "awaiting_budget"}:
                    if action["tool_id"] not in {
                        tool.id for tool in self._available_tools(execution)
                    }:
                        self.store.finish_turn(turn_id, turn["status"])
                        raise TurnFailed(
                            "The stored action is outside current selected tool availability.",
                            turn_id,
                        )
                    if job_id:
                        try:
                            actions.bind_job(self.store, action["id"], job_id)
                        except ValueError as exc:
                            self.store.finish_turn(turn_id, turn["status"])
                            raise TurnFailed(str(exc), turn_id) from exc
                        action["job_id"] = job_id
                    turn_control.guard(self.store, execution)
                    actions.state(self.store, action["id"], "dispatching")
                    outcome = self.toolgate.invoke(
                        action["tool_id"],
                        action["args"],
                        approval_request_id=turn["approval_request_id"],
                        action_id=action["id"],
                        job_id=action["job_id"],
                    )
                else:
                    outcome = self.toolgate.check_action(action["id"], action["tool_id"])
            except ProviderUnavailable as exc:
                self.store.finish_turn(
                    turn_id,
                    "acted_no_reply" if acted else "failed",
                    acted=int(acted),
                    detail=exc.reason,
                )
                if acted:
                    raise ActedWithoutReply(turn_id, exc.reason) from exc
                raise TurnFailed(exc.reason, turn_id) from exc
            except (ToolRefused, ToolGateUnavailable) as exc:
                if isinstance(exc, ToolRefused) and exc.code == "BUDGET_DENIED":
                    return self._hold(
                        turn_id,
                        ToolPending(
                            "awaiting_budget",
                            "Owner spending configuration required: " + exc.message,
                            action["id"],
                        ),
                    )
                actions.state(self.store, action["id"], "refused")
                reason = getattr(exc, "message", None) or getattr(exc, "reason", type(exc).__name__)
                self.store.finish_turn(
                    turn_id,
                    "acted_no_reply" if acted else "failed",
                    detail=reason,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                if acted:
                    raise ActedWithoutReply(turn_id, reason) from exc
                raise TurnFailed(reason, turn_id) from exc

            if isinstance(outcome, ToolPending):
                return self._hold(turn_id, outcome)

            if isinstance(outcome, ApprovalRequired):
                actions.state(self.store, action["id"], "awaiting_approval")
                # ToolGate asked again, so the approval did not apply - expired,
                # or never granted. Saying "done" here would be the worst
                # possible lie. The turn stays parked, now on the new request:
                # the old nonce is dead, and keeping it stored would park this
                # turn forever behind an id nobody can ever approve.
                self.store.finish_turn(
                    turn_id,
                    "awaiting_approval",
                    approval_request_id=outcome.request_id,
                    approval_tool_id=outcome.tool_id,
                    approval_args=json.dumps(outcome.args, ensure_ascii=False),
                    approval_expires_at=outcome.expires_at,
                    detail=outcome.message,
                )
                raise TurnFailed(
                    "that approval is no longer valid; the action must be confirmed again"
                )

            # The action has happened and the approval is spent. That is written
            # down now, before anything else is attempted, because everything
            # after this point can fail and none of it can un-happen the action.
            actions.record(self.store, action, outcome)
            acted = acted or outcome.ok

        if execution.get("researchMode") == "deep" and turn["status"] in {
            "awaiting_approval",
            "awaiting_budget",
            "action_in_progress",
            "outcome_unknown",
        }:
            with self.store._connect() as db:
                source = db.execute(
                    "SELECT m.content FROM turn_messages tm JOIN messages m ON m.id=tm.message_id "
                    "WHERE tm.turn_id=? AND tm.purpose='input'",
                    (turn_id,),
                ).fetchone()
            return self._run_bound(
                session_id, json.loads(source[0]), turn_id, {}, None, continued=True
            )
        return self._finish_reply(turn_id, session_id, execution, acted, started)

    def recover_reply(self, turn_id, request_id):
        turn, created = turn_control.claim_reply(self.store, turn_id, request_id)
        if not created:
            return {
                "turn_id": turn_id,
                "session_id": turn["session_id"],
                "request_id": request_id,
                "status": turn["status"],
                "acted": bool(turn["acted"]),
                "replayed": True,
            }
        try:
            execution = session_settings.execution(self.store, turn["session_id"], turn_id)
            execution["replyRecoveryId"] = request_id
            return {
                **self._finish_reply(
                    turn_id, turn["session_id"], execution, True, time.monotonic(), request_id
                ),
                "request_id": request_id,
                "replayed": False,
            }
        except Exception:
            self.store.finish_turn(turn_id, "acted_no_reply", acted=1)
            raise

    def _finish_reply(self, turn_id, session_id, execution, acted, started, reply_request_id=None):
        try:
            # This path narrates an already recorded action. It never dispatches
            # another tool, so advertising tools here misleads the answer model.
            history = self._history(session_id, turn_id=turn_id, reply_only=True)
            ctx = TurnContext(history_chars=history_size(history), needs_tools=False)
            route, completion, _skipped = self._call(history, ctx, execution)
            from pi.context import research

            research.validate_narration(execution, completion.text)
            tool_protocol.validate_answer(completion.text)
            turn_control.guard(self.store, execution)
        except (ProviderUnavailable, RuntimeError) as exc:
            reason = getattr(exc, "reason", type(exc).__name__)
            # Not "failed". The tool ran, and a record saying otherwise would
            # tell the owner their action did not happen when it did. Resuming
            # again asks only for the reply.
            self.store.finish_turn(
                turn_id,
                "acted_no_reply" if acted else "failed",
                acted=int(acted),
                detail=reason,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            if acted:
                raise ActedWithoutReply(turn_id, reason) from exc
            raise TurnFailed(reason, turn_id) from exc

        message = self._complete_turn(
            turn_id,
            completion.text,
            citations=completion.citations,
            acted=int(acted),
            reply_request_id=reply_request_id,
            provider=completion.provider,
            model=completion.model,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cached_tokens=completion.cached_tokens,
            cost_usd=completion.cost_usd,
            latency_ms=int((time.monotonic() - started) * 1000),
            route_tier=route.tier.value,
            route_reason=route.reason.value,
            detail=route.detail(_skipped),
        )
        return {
            "session_id": session_id,
            "turn_id": turn_id,
            "status": "complete",
            "acted": acted,
            "message": message,
            "memory": self.memory.status(session_id, turn_id),
        }

    def _hold(self, turn_id, outcome):
        if outcome.action_id:
            actions.state(self.store, outcome.action_id, outcome.status)
        self.store.finish_turn(turn_id, outcome.status, detail=outcome.message)
        turn = self.store.get_turn(turn_id)
        return {
            "turn_id": turn_id,
            "session_id": turn["session_id"],
            "status": outcome.status,
            "acted": bool(turn["acted"]),
            "action_id": outcome.action_id,
            "message": None,
            "notice": outcome.message,
            "memory": self.memory.status(turn["session_id"], turn_id),
        }

    def _complete_turn(self, turn_id, text, **fields):
        """A stop racing final persistence must leave a truthful terminal turn."""
        try:
            return self.store.complete_turn(turn_id, text, **fields)
        except turn_steering.Pending:
            from pi.models.providers import Completion

            turn_steering.discarded(
                self.store,
                turn_id,
                Completion(
                    text="",
                    model=fields.get("model", "unknown"),
                    provider=fields.get("provider", "unknown"),
                    **{
                        key: fields.get(key)
                        for key in ("input_tokens", "output_tokens", "cached_tokens", "cost_usd")
                    },
                ),
            )
            raise
        except ProviderUnavailable as exc:
            acted = bool((self.store.get_turn(turn_id) or {}).get("acted"))
            self.store.finish_turn(
                turn_id,
                "acted_no_reply" if acted else "failed",
                acted=int(acted),
                detail=exc.reason,
            )
            if acted:
                raise ActedWithoutReply(turn_id, exc.reason) from exc
            raise TurnFailed(exc.reason, turn_id) from exc
