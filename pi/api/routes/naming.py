"""Chat name routes: rename, auto-name, and read the naming state; archive or trash chats."""

from fastapi import APIRouter, Depends, HTTPException

from pi.conversation import archive, naming


def router(get_store, get_providers, authorize):
    routes = APIRouter(prefix="/sessions", dependencies=[Depends(authorize)])

    @routes.post("/archive")
    def archive_chats(body: archive.Archive):
        return archive.apply(get_store(), body)

    @routes.post("/trash")
    def trash_chats(body: archive.Trash):
        return archive.trash(get_store(), body)

    @routes.get("/{session_id}/title")
    def get_title(session_id: str):
        return naming.load(get_store(), session_id)

    @routes.post("/{session_id}/title")
    def set_title(session_id: str, body: naming.Rename):
        try:
            return naming.rename(get_store(), session_id, body)
        except ValueError as exc:
            raise HTTPException(422, {"code": "invalid_title", "message": str(exc)}) from None

    @routes.post("/{session_id}/title/auto")
    def auto_title(session_id: str, body: naming.AutoName):
        return naming.auto_name(get_store(), get_providers(), session_id, body)

    return routes
