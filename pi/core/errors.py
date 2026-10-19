"""One refusal shape for the whole API."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ServiceError(Exception):
    """A refusal Pi reports as ``{"detail": {"code", "message", ...}}`` with an HTTP status.

    Each package keeps its own subclass, so a raise names where it came from; the shape, the
    status and the one handler that ``install`` adds are shared.
    """

    def __init__(self, code: str, message: str, status: int = 409, **extra):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.detail = {"code": code, "message": message, **extra}


def install(app: FastAPI) -> None:
    """Answer every ServiceError the app raises in its one shape."""
    app.add_exception_handler(ServiceError, _respond)


async def _respond(_request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)
