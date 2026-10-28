"""His answers from the Inbox: a decision on a request continues the work; a question gets answered.

`/approvals/{id}/decided` is called by the gateway after ToolGate recorded his decision; Pi grants
nothing (ToolGate checks the approval as it is spent) and a "no" only closes. See
`pi.turns.answers` and `pi.agents.asks`.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path
from pydantic import Field

from pi.agents import asks
from pi.core.models import Strict
from pi.schedule import jobs
from pi.turns import answers

RequestPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
AskPath = Annotated[str, Path(pattern=asks.ASK_ID)]


class Decided(Strict):
    status: Literal["approved", "rejected", "dismissed"]
    note: str = Field(default="", max_length=2000)


class Answer(Strict):
    text: str = Field(min_length=1, max_length=4000)
    # He tapped one of Conker's options (the AI's words), rather than writing his own.
    chosen: bool = False


def router(store, loop, authorize, executor=lambda: None):
    routes = APIRouter(dependencies=[Depends(authorize)])

    @routes.post("/approvals/{request_id}/decided")
    def decided(request_id: RequestPath, body: Decided):
        """Continue what waits on this request: run it, or close it and pass on his words."""
        adapter = executor()
        return answers.decided(
            loop(),
            request_id,
            body.status,
            body.note,
            resume_run=(
                (lambda run_id: jobs.dispatch_claim(store(), {"id": run_id}, adapter, resume=True))
                if adapter is not None
                else None
            ),
            cancel_run=lambda run_id: jobs.cancel_run(store(), run_id),
        )

    @routes.get("/asks")
    def listing():
        """Conker's questions waiting for him, oldest first."""
        return {"schemaVersion": 1, "results": asks.listing(store())}

    @routes.get("/asks/{identity}")
    def get(identity: AskPath):
        return asks.get(store(), identity)

    @routes.post("/asks/{identity}/answer")
    def answer(identity: AskPath, body: Answer):
        """Record his answer; it goes to that chat as his next message."""
        ask = asks.answer(store(), identity, body.text, chosen=body.chosen)
        answers.answered(loop(), ask)
        return ask

    @routes.post("/asks/{identity}/dismiss")
    def dismiss(identity: AskPath):
        """No answer: nothing goes back to the chat."""
        return asks.dismiss(store(), identity)

    return routes
