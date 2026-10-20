"""The owner terminal: open, read, write and close an isolated session."""

from __future__ import annotations

import base64

from fastapi import APIRouter, Query, Request

from .operations import json_body
from .store import AuthError


def router(app, session, admit_write):
    routes = APIRouter()

    def terminal_access(request):
        owner = session(request)
        if app.state.terminals is None:
            raise AuthError("Owner terminal is not configured.", 503)
        return owner, app.state.terminals

    @routes.post("/api/terminal")
    async def terminal_create(request: Request):
        owner, manager = terminal_access(request)
        body = await json_body(request)
        if set(body) != {"requestId"} or request.scope["query_string"]:
            raise AuthError("Supply only a terminal requestId.", 422)
        token = request.cookies.get(app.state.config.cookie_name, "")

        def validate():
            current = app.state.auth.session(token)
            if current["id"] != owner["id"]:
                raise AuthError("Terminal session identity changed.")

        return manager.create(
            owner["id"], body["requestId"], validate, lambda: admit_write(request, body)
        )

    @routes.get("/api/terminal/current")
    def terminal_current(request: Request):
        owner, manager = terminal_access(request)
        return {
            "lease": manager.current(owner["id"]),
            "workspace": app.state.config.terminal_workspace_label,
            "maximumLifetimeSeconds": 900,
            "commandsPersisted": False,
            "outputPersisted": False,
        }

    @routes.get("/api/terminal/{identity}")
    def terminal_read(identity: str, request: Request, cursor: int = Query(default=0, ge=0)):
        owner, manager = terminal_access(request)
        result = manager.use(owner["id"], identity, "read", cursor)
        return {
            **result,
            "data": base64.b64encode(result["data"]).decode("ascii"),
            "encoding": "base64",
        }

    @routes.post("/api/terminal/{identity}/{operation}")
    async def terminal_write(identity: str, operation: str, request: Request):
        owner, manager = terminal_access(request)
        body = await json_body(request)
        if request.scope["query_string"]:
            raise AuthError("Terminal writes cannot include query parameters.", 422)
        if operation == "input" and set(body) == {"data"} and isinstance(body["data"], str):
            try:
                data = base64.b64decode(body["data"], validate=True)
            except ValueError:
                raise AuthError("Use base64 terminal input.", 422) from None
            return {
                "acceptedBytes": manager.use(owner["id"], identity, "write", data),
                "automaticReplay": False,
            }
        if operation == "resize" and set(body) == {"rows", "columns"}:
            manager.use(owner["id"], identity, "resize", body["rows"], body["columns"])
            return {"resized": True}
        if operation == "close" and not body:
            manager.use(owner["id"], identity, "close")
            return {"closed": True}
        raise AuthError("Invalid terminal operation.", 422)

    return routes
