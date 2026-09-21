from fastapi import APIRouter, Depends, HTTPException, Query

from . import drafts


def router(store, authorize):
    routes = APIRouter(prefix="/drafts", dependencies=[Depends(authorize)])

    def call(function, *args):
        try:
            return function(store(), *args)
        except drafts.DraftError as exc:
            raise HTTPException(409, str(exc)) from exc

    @routes.get("/{session_id}")
    def load(session_id: str, task_id: str | None = Query(default=None, max_length=128)):
        return call(drafts.load, session_id, task_id)

    @routes.put("/{session_id}")
    def save(
        session_id: str,
        body: drafts.Save,
        task_id: str | None = Query(default=None, max_length=128),
    ):
        return call(drafts.save, session_id, body, task_id)

    return routes
