from fastapi import APIRouter, Depends, HTTPException

from pi.context import decisions


def router(store, authorize):
    routes = APIRouter(prefix="/context", dependencies=[Depends(authorize)])

    def invoke(fn, *args):
        try:
            return fn(store(), *args)
        except decisions.DecisionError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from None

    @routes.get("/{session_id}/items")
    def get_items(session_id: str):
        return invoke(decisions.load, session_id)

    @routes.post("/{session_id}/items")
    def add_item(session_id: str, body: decisions.Add):
        return invoke(decisions.add, session_id, body)

    @routes.post("/{session_id}/items/{item_id}/state")
    def change_item_state(session_id: str, item_id: str, body: decisions.State):
        return invoke(decisions.change_state, session_id, item_id, body)

    return routes
