"""One refusal shape for the whole API."""


class ServiceError(Exception):
    """A refusal Pi reports as ``{"detail": {"code", "message", ...}}`` with an HTTP status.

    Each package keeps its own subclass, so a raise names where it came from; the shape, the
    status and the one exception handler in ``pi.api.server`` are shared.
    """

    def __init__(self, code: str, message: str, status: int = 409, **extra):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.detail = {"code": code, "message": message, **extra}
