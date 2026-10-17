from fastapi import APIRouter, Depends, HTTPException

from pi.conversation import output_settings


def router(store, authorize):
    routes = APIRouter(prefix="/sessions", dependencies=[Depends(authorize)])

    def invoke(fn, *args):
        try:
            return fn(store(), *args)
        except output_settings.OutputError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from None

    @routes.get("/{session_id}/output")
    def get(session_id: str):
        return invoke(output_settings.load, session_id)

    @routes.post("/{session_id}/output")
    def save(session_id: str, body: output_settings.Update):
        return invoke(output_settings.save, session_id, body)

    return routes
