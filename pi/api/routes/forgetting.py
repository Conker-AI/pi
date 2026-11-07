"""Deleting chats forever from the owner's control plane (owner credential only).

The reading shows what each chat's deletion reaches (counts, never text) and the confirmation
to send back. The write checks every confirmation against a fresh preview, queues the chats,
answers at once and restarts Pi when no turn is in progress; the offline forgetting command runs them before Pi opens its database again
(pi.conversation.forget_queue). It needs a supervisor that restarts Pi (PI_SUPERVISED).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import Field, StringConstraints, model_validator

from pi.conversation import forget_queue
from pi.core.models import Strict

SESSION = r"^[A-Za-z0-9_-]{1,128}$"
SessionId = Annotated[str, StringConstraints(pattern=SESSION)]


class ForgetChat(Strict):
    session: str = Field(pattern=SESSION)
    confirmation: str = Field(pattern=r"^[0-9a-f]{64}$")


class ForgetChats(Strict):
    chats: list[ForgetChat] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _unique(self):
        if len({chat.session for chat in self.chats}) != len(self.chats):
            raise ValueError("each chat may appear once")
        return self


def _plans(db, sessions: list[str]) -> dict:
    plans = forget_queue.preview_many(db, sessions)
    missing = [session for session in sessions if "error" in plans[session]]
    if missing:
        raise forget_queue.ForgetRefused(
            "not_found", "These chats cannot be deleted.", 404, sessions=missing
        )
    return plans


def router(state, authorize):
    routes = APIRouter(prefix="/forgetting", dependencies=[Depends(authorize)])

    @routes.get("/chats")
    def overview(session: Annotated[list[SessionId] | None, Query(max_length=50)] = None):
        """What deleting each named chat reaches, and the chats queued or finished."""
        db = state().store.path
        names = list(dict.fromkeys(session or []))
        plans = _plans(db, names) if names else {}
        chats = [forget_queue.summary(sid, plan) for sid, plan in plans.items()]
        return {**forget_queue.overview(db), "chats": chats}

    @routes.post("/chats")
    def delete(body: ForgetChats):
        """Delete chats forever: queue them and restart Pi, which forgets them before it opens."""
        if not forget_queue.supervised():
            raise forget_queue.ForgetRefused(
                "not_supervised",
                "Nothing restarts Pi here, so it cannot delete chats itself. "
                "Use the offline command in docs/forgetting.md.",
            )
        db = state().store.path
        sessions = [chat.session for chat in body.chats]
        plans = _plans(db, sessions)
        changed = [
            chat.session
            for chat in body.chats
            if plans[chat.session]["confirmation"] != chat.confirmation
        ]
        if changed:
            raise forget_queue.ForgetRefused(
                "scope_changed", forget_queue.CHANGED, sessions=changed
            )
        waiting = [
            {"sessionId": sid, "sessionIds": sorted(plans[sid]["session_ids"])}
            for sid in sessions
            if not plans[sid].get("already_forgotten")
        ]
        if not waiting:
            return {"state": "done", "sessions": sessions}
        forget_queue.queue(db, waiting)
        getattr(state(), "forget_restart", forget_queue.restart_soon)(db)
        return JSONResponse(
            {"state": "restarting", "sessions": [item["sessionId"] for item in waiting]},
            status_code=202,
        )

    return routes
