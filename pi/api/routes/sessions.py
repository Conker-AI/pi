"""Sessions, turns and submissions: create and read sessions, run, resume, cancel and stream turns, approvals and reply recovery."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from pi.conversation import submissions
from pi.turns import control as turn_control
from pi.turns import thinking as background_thinking
from pi.turns.failures import ActedWithoutReply, TurnFailed


class NewSession(BaseModel):
    title: str = ""
    agent_id: str | None = Field(default=None, pattern=r"^(?:companion|agent_[0-9a-f]{32})$")
    privacy: dict[Literal["memoryDisabled", "harnessDisabled"], bool] | None = None


class TurnRequest(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=16000,
        description="Send at most 16000 characters per message; split longer text.",
    )
    # Routing hints the caller genuinely knows. The router is deliberately not
    # a classifier that reads the message - that would be a model nobody
    # evaluates deciding how much every turn costs.
    needs_tools: bool = False
    is_analysis: bool = False
    owner_requested_strong: bool = False
    request_id: str | None = Field(
        default=None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    task_expected_revision: int | None = Field(default=None, ge=1, strict=True)
    draft_revision: int | None = Field(default=None, ge=1, strict=True)
    attachment_ids: list[str] = Field(default_factory=list)
    model_id: str | None = Field(default=None, min_length=1, max_length=200)
    reply_to: str | None = Field(default=None, min_length=1, max_length=200)
    research_mode: Literal["off", "web", "deep"] = "off"
    skill_ids: list[str] = Field(default_factory=list, max_length=3)
    think: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def task_submission(self):
        if (self.task_id is None) != (self.task_expected_revision is None):
            raise ValueError("Task identity and revision belong together.")
        if self.task_id is not None and self.request_id is None:
            raise ValueError("Task-bound submissions require a retained request identity.")
        return self


class ResumeRequest(BaseModel):
    job_id: str | None = Field(default=None, min_length=1, max_length=100)


class ReplyRecoveryRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def router(app_state, require_key, require_admin):
    routes = APIRouter()

    def _acted_without_reply(exc: ActedWithoutReply) -> dict:
        """The one answer that is true when a tool ran and the model then did not.

        Not an error status, and not `complete` either. The action happened, it is
        recorded, and the caller is told plainly that the reply is what is missing
        and where to ask for it again.
        """
        return {
            "turn_id": exc.turn_id,
            "status": "acted_no_reply",
            "acted": True,
            "memory": app_state().loop.memory.status(
                app_state().store.get_turn(exc.turn_id)["session_id"], exc.turn_id
            ),
            "message": None,
            "detail": exc.cause,
            "hint": f"the action ran and was recorded; POST /turns/{exc.turn_id}/resume "
            "to ask for the reply again - it will not run the action a second time",
        }

    @routes.post("/sessions", dependencies=[Depends(require_key)])
    def create_session(body: NewSession):
        return {
            "session_id": app_state().store.create_session(
                title=body.title, agent_id=body.agent_id, privacy=body.privacy
            )
        }

    @routes.get("/sessions", dependencies=[Depends(require_key)])
    def list_sessions(limit: int = 50):
        return {"results": app_state().store.list_sessions(limit=min(max(limit, 1), 200))}

    @routes.get("/sessions/history", dependencies=[Depends(require_key)])
    def session_history_page(request: Request):
        from pi.conversation import history as session_history

        return JSONResponse(
            session_history.page(
                app_state().store, **session_history.request_arguments(request.query_params)
            ),
            headers={"Cache-Control": "no-store"},
        )

    @routes.get("/sessions/{session_id}", dependencies=[Depends(require_key)])
    def get_session(session_id: str):
        session = app_state().store.get_session(session_id)
        if session is None:
            raise HTTPException(404, "no such session")
        pending = submissions.list_pending(app_state().store, session_id, limit=100)
        return {
            **session,
            "pending_submissions": pending["results"],
            "pending_submissions_truncated": pending["next_cursor"] is not None,
            "messages": app_state().store.messages(session_id),
            "turns": [
                {**turn, "memory": app_state().loop.memory.status(session_id, turn["id"])}
                for turn in app_state().store.turns(session_id)
            ],
            "memory": app_state().loop.memory.status(session_id),
            "thinking": background_thinking.list_for_session(app_state().store, session_id),
        }

    def _memory_delivery_status(session_id, request, message_id=None):
        from pi.memory import delivery as memory_delivery

        if request.url.query or any(
            not 1 <= len(value) <= 128
            or not all(
                character.isascii() and (character.isalnum() or character in "_-")
                for character in value
            )
            for value in (session_id, message_id)
            if value is not None
        ):
            raise HTTPException(422, "Invalid memory delivery request.")
        try:
            return memory_delivery.read(app_state().store, session_id, message_id)
        except ValueError:
            raise HTTPException(404, "Memory delivery unavailable.") from None

    @routes.get("/sessions/{session_id}/memory-delivery", dependencies=[Depends(require_key)])
    def memory_delivery_status(session_id: str, request: Request):
        return _memory_delivery_status(session_id, request)

    @routes.get(
        "/sessions/{session_id}/messages/{message_id}/memory-delivery",
        dependencies=[Depends(require_key)],
    )
    def message_memory_delivery_status(session_id: str, message_id: str, request: Request):
        return _memory_delivery_status(session_id, request, message_id)

    @routes.get("/sessions/{session_id}/observation", dependencies=[Depends(require_key)])
    def observe_session(session_id: str, request: Request):
        from pi.conversation import observation as session_observation

        if (
            request.url.query
            or not 1 <= len(session_id) <= 128
            or not all(
                character.isascii() and (character.isalnum() or character in "_-")
                for character in session_id
            )
        ):
            raise HTTPException(422, "Invalid conversation observation request.")
        try:
            return JSONResponse(
                session_observation.read(app_state().store, session_id),
                headers={"Cache-Control": "no-store"},
            )
        except ValueError:
            raise HTTPException(404, "Conversation not found.") from None
        except OverflowError:
            raise HTTPException(503, "Conversation observation unavailable.") from None

    @routes.get("/sessions/{session_id}/thinking", dependencies=[Depends(require_key)])
    def thinking_status(session_id: str):
        session = app_state().store.get_session(session_id)
        if session is None:
            raise HTTPException(404, "no such session")
        return {
            "schemaVersion": 1,
            "session_id": session_id,
            "content_status": "forgotten" if session["status"] == "forgotten" else "available",
            "results": background_thinking.list_for_session(app_state().store, session_id),
        }

    @routes.get("/turns/{turn_id}/usage", dependencies=[Depends(require_key)])
    def turn_usage(turn_id: str, request: Request):
        from pi.context import research_usage

        if (
            request.url.query
            or not 1 <= len(turn_id) <= 128
            or not all(
                character.isascii() and (character.isalnum() or character in "_-")
                for character in turn_id
            )
        ):
            raise HTTPException(422, "Invalid turn usage request.")
        try:
            return research_usage.receipt(app_state().store, turn_id)
        except ValueError:
            raise HTTPException(404, "Turn usage unavailable.") from None

    @routes.post(
        "/sessions/{session_id}/thinking/{turn_id}/cancel", dependencies=[Depends(require_key)]
    )
    def cancel_thinking(session_id: str, turn_id: str):
        try:
            return background_thinking.cancel(app_state().store, session_id, turn_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @routes.post("/sessions/{session_id}/turns", dependencies=[Depends(require_key)])
    def run_turn(session_id: str, body: TurnRequest, background: BackgroundTasks):
        try:
            result = app_state().loop.run_turn(
                session_id,
                body.text,
                context={
                    "needs_tools": body.needs_tools,
                    "is_analysis": body.is_analysis,
                    "owner_requested_strong": body.owner_requested_strong,
                },
                request_id=body.request_id,
                task_id=body.task_id,
                task_expected_revision=body.task_expected_revision,
                **(
                    {"draft_revision": body.draft_revision}
                    if body.draft_revision is not None
                    else {}
                ),
                **({"attachment_ids": body.attachment_ids} if body.attachment_ids else {}),
                **({"model_id": body.model_id} if body.model_id is not None else {}),
                **({"reply_to": body.reply_to} if body.reply_to is not None else {}),
                **({"research_mode": body.research_mode} if body.research_mode != "off" else {}),
                **({"skill_ids": body.skill_ids} if body.skill_ids else {}),
                **({"think": True} if body.think else {}),
            )
        except ActedWithoutReply as exc:
            # Deliberately not an error status. A tool ran, so this request did the
            # thing that actually matters, and the one detail missing is what the
            # model would have said about it. An error code would invite a retry,
            # and retrying this turn would run the action a second time.
            result = _acted_without_reply(exc)
            if body.request_id:
                result["submission"] = submissions.get(app_state().store, body.request_id)
            return result
        except TurnFailed as exc:
            turn = app_state().store.get_turn(exc.turn_id) if exc.turn_id else None
            if turn and turn["status"] == "cancelled":
                result = {
                    "turn_id": exc.turn_id,
                    "session_id": turn["session_id"],
                    "status": "cancelled",
                    "acted": bool(turn["acted"]),
                    "message": None,
                }
                if body.request_id:
                    result["submission"] = submissions.get(app_state().store, body.request_id)
                return result
            # 503, not 500: the provider did not answer, which is a state the caller
            # can act on. The user's message is already stored either way.
            raise HTTPException(
                503,
                {
                    "message": f"turn failed: {exc.reason}",
                    "turn_id": exc.turn_id,
                    "memory": app_state().loop.memory.status(session_id, exc.turn_id),
                    "request_id": body.request_id,
                },
            ) from exc
        if (
            isinstance(result, dict)
            and result.get("message") is not None
            and result.get("session_id")
        ):
            from pi.conversation import naming

            background.add_task(
                naming.after_turn,
                app_state().store,
                lambda: app_state().router.adapters(),
                result["session_id"],
            )
        return result

    @routes.get("/turn-submissions/{request_id}", dependencies=[Depends(require_key)])
    def get_submission(request_id: str):
        return submissions.get(app_state().store, request_id)

    @routes.get("/turn-submissions/{request_id}/stream", dependencies=[Depends(require_key)])
    async def stream_submission(request_id: str, request: Request, after: int = Query(0, ge=0)):
        """Server-sent preview of an answer being written; the saved message stays authoritative.

        Events: `delta` (text to append), `reset` (discard the preview: a new model
        attempt started), `done` (fetch the saved turn). Unknown or expired previews
        end with `unavailable`, which is not an error: the saved turn is still there.
        """
        from pi.conversation import live_stream

        async def events():
            import asyncio

            seen, waited, idle = after, 0.0, 0.0
            while True:
                if await request.is_disconnected():
                    return
                state = live_stream.read(request_id, seen)
                if state is None:
                    if waited >= 15.0:
                        yield "event: unavailable\ndata: {}\n\n"
                        return
                    waited += 0.05
                else:
                    batch, finished = state
                    for event in batch:
                        seen = event["seq"]
                        kind = event.pop("type")
                        yield f"id: {seen}\nevent: {kind}\ndata: {json.dumps(event)}\n\n"
                    if batch:
                        idle = 0.0
                    if finished and not batch:
                        return
                idle += 0.05
                if idle >= 15.0:
                    idle = 0.0
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.05)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @routes.get("/turns/{turn_id}/research", dependencies=[Depends(require_key)])
    def get_research_receipt(turn_id: str):
        from pi.context import research

        return research.receipt(app_state().store, turn_id)

    @routes.get("/sessions/{session_id}/submissions", dependencies=[Depends(require_key)])
    def list_pending_submissions(
        session_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=128),
    ):
        return submissions.list_pending(app_state().store, session_id, limit, cursor)

    @routes.get("/turns/unreplied", dependencies=[Depends(require_key)])
    def unreplied():
        """Turns that acted but never reported back.

        Resumable, and resuming asks only for the missing reply - the action is
        never repeated. Separate from /approvals on purpose: these need a retry,
        not a decision.
        """
        return {"results": app_state().store.acted_without_reply()}

    @routes.get("/approvals", dependencies=[Depends(require_key)])
    def approvals():
        """Every turn parked on the owner, across all sessions.

        One queue rather than a per-session hunt: an approval the owner never sees
        is an action that silently never happens.
        """
        return {"results": app_state().store.awaiting_approval()}

    @routes.post("/turns/{turn_id}/resume", dependencies=[Depends(require_key)])
    def resume(turn_id: str, body: ResumeRequest | None = None):
        """Continue a parked turn after the owner approved it in ToolGate.

        Pi does not grant approvals and does not hold them. This replays the exact
        stored action; ToolGate consumes the nonce, once, and refuses a replay.
        """
        try:
            return app_state().loop.resume_turn(turn_id, job_id=body.job_id if body else None)
        except ActedWithoutReply as exc:
            return _acted_without_reply(exc)
        except TurnFailed as exc:
            raise HTTPException(
                409,
                {
                    "message": f"cannot resume: {exc.reason}",
                    "memory": app_state().loop.memory.status(turn_id=turn_id),
                },
            ) from exc

    @routes.post("/sessions/{session_id}/fork", dependencies=[Depends(require_key)])
    def fork_session(session_id: str):
        if app_state().store.get_session(session_id) is None:
            raise HTTPException(404, "no such session")
        try:
            return {"session_id": app_state().loop.fork(session_id), "parent_id": session_id}
        except TurnFailed as exc:
            raise HTTPException(409, exc.reason) from exc

    @routes.get("/messages/{message_id}", dependencies=[Depends(require_key)])
    def get_message(message_id: str):
        message = app_state().store.get_message(message_id)
        if message is None:
            raise HTTPException(404, "no such message")
        return message

    @routes.post("/turns/{turn_id}/cancel", dependencies=[Depends(require_admin)])
    def cancel_turn(turn_id: str):
        return turn_control.cancel(app_state().store, turn_id)

    @routes.post("/turn-submissions/{request_id}/cancel", dependencies=[Depends(require_key)])
    def cancel_submission(request_id: str):
        """Stop is an owner safety action: record it durably, then end any live answer stream."""
        from pi.conversation import live_stream

        result = turn_control.cancel_submission(app_state().store, request_id)
        live_stream.stop(request_id)
        return result

    @routes.post("/turns/{turn_id}/reply-only", dependencies=[Depends(require_admin)])
    def recover_turn_reply(turn_id: str, body: ReplyRecoveryRequest):
        try:
            return app_state().loop.recover_reply(turn_id, body.request_id)
        except ActedWithoutReply as exc:
            return {**_acted_without_reply(exc), "request_id": body.request_id}

    return routes
