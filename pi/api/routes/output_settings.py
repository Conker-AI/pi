from fastapi import APIRouter, Depends

from pi.conversation import output_settings
from pi.core.models import described


def router(store, authorize):
    routes = APIRouter(prefix="/sessions", dependencies=[Depends(authorize)])
    document = described(output_settings.OutputDocument)

    def invoke(fn, *args):
        return fn(store(), *args)

    @routes.get("/{session_id}/output", responses=document)
    def get(session_id: str):
        return invoke(output_settings.load, session_id)

    @routes.post("/{session_id}/output", responses=document)
    def save(session_id: str, body: output_settings.Update):
        return invoke(output_settings.save, session_id, body)

    return routes
