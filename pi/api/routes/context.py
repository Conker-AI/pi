from fastapi import APIRouter, Depends, HTTPException

from pi.context import controls, retrieval, summaries


def router(store, authorize):
    routes = APIRouter(prefix="/context", dependencies=[Depends(authorize)])

    def invoke(fn, *args):
        return fn(store(), *args)

    @routes.get("/{session_id}")
    def get(session_id: str):
        return invoke(controls.load, session_id)

    @routes.post("/{session_id}")
    def save(session_id: str, body: controls.Update):
        return invoke(controls.save, session_id, body)

    @routes.get("/{session_id}/turns/{turn_id}")
    def snapshot(session_id: str, turn_id: str):
        return invoke(controls.load, session_id, turn_id)

    @routes.get("/{session_id}/selections/{request_id}")
    def selection(session_id: str, request_id: str):
        # Read-only inspection never starts or resumes a helper.
        value = retrieval.read(store(), session_id, request_id=request_id)
        if value is None:
            raise HTTPException(404, "Context selection not found.")
        return value

    @routes.post("/{session_id}/fork")
    def fork(session_id: str, body: controls.ReviewedFork):
        return invoke(controls.reviewed_fork, session_id, body)

    @routes.get("/{session_id}/summary")
    def summary(session_id: str):
        return invoke(summaries.load, session_id)

    @routes.post("/{session_id}/summary")
    def edit_summary(session_id: str, body: summaries.Edit):
        return invoke(summaries.save, session_id, body)

    @routes.post("/{session_id}/summary/restore")
    def restore_summary(session_id: str, body: summaries.Restore):
        return invoke(summaries.save, session_id, body)

    return routes
