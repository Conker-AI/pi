"""Output settings API routes."""

from fastapi import APIRouter, Depends, HTTPException

from pi.conversation import output_settings


def router(store, authorize):
    """Create output settings router."""
    routes = APIRouter(prefix="/sessions", dependencies=[Depends(authorize)])

    @routes.get("/{session_id}/output")
    async def get_output_settings(session_id: str):
        """Get output settings for a session."""
        try:
            return output_settings.load(store, session_id)
        except output_settings.OutputError as exc:
            raise HTTPException(
                status_code=exc.status, detail={"code": exc.code, "message": exc.message}
            ) from exc

    @routes.post("/{session_id}/output")
    async def update_output_settings(session_id: str, body: output_settings.Update):
        """Update output settings for a session."""
        try:
            return output_settings.save(store, session_id, body)
        except output_settings.OutputError as exc:
            raise HTTPException(
                status_code=exc.status, detail={"code": exc.code, "message": exc.message}
            ) from exc

    return routes
