"""Owner sign-in: session, login, verification, logout and session revocation."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .operations import fingerprint, json_body
from .store import AuthError


def router(app, session, cookie, admit_write):
    routes = APIRouter()

    @routes.get("/auth/session")
    def auth_session(request: Request):
        try:
            value = session(request, authenticated=False)
        except AuthError:
            value = app.state.auth.anonymous()
        response = JSONResponse(
            {
                "authenticated": bool(value["authenticated"]),
                "csrf_token": value["csrf"],
                "session_id": value["id"],
                "expires_at": value["expires"],
                "unlock_expires_at": value["unlock_expires_at"],
                "setup_required": not app.state.auth.configured(),
            }
        )
        return cookie(response, value["token"]) if "token" in value else response

    @routes.post("/auth/login")
    async def login(request: Request):
        session(request, authenticated=False)
        body = await json_body(request)
        if set(body) != {"password"} or not isinstance(body["password"], str):
            raise AuthError("Send a password in a JSON object.", 422)
        # FastAPI runs sync routes in a threadpool; do the expensive check there too.
        from starlette.concurrency import run_in_threadpool

        value = await run_in_threadpool(
            app.state.auth.login,
            request.cookies.get(app.state.config.cookie_name, ""),
            body["password"],
            request.client.host if request.client else "unknown",
        )
        return cookie(
            JSONResponse(
                {
                    "authenticated": True,
                    "csrf_token": value["csrf"],
                    "session_id": value["id"],
                    "expires_at": value["expires"],
                    "unlock_expires_at": value["unlock_expires_at"],
                }
            ),
            value["token"],
        )

    @routes.post("/auth/verify")
    async def verify(request: Request):
        session(request)
        # A verified Character Studio operation may carry bounded embedded media.
        # The caller is already an authenticated owner; fingerprint still rejects
        # every route outside the explicit browser contract.
        body = await json_body(request, max_bytes=66 * 1024 * 1024)
        operation = body.get("operation")
        if (
            set(body) != {"password", "operation"}
            or not isinstance(body["password"], str)
            or not isinstance(operation, dict)
            or set(operation) != {"method", "path", "body"}
        ):
            raise AuthError("Send a password and the complete operation to verify.", 422)
        binding = fingerprint(operation["method"], operation["path"], operation["body"])
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            app.state.auth.verify,
            request.cookies.get(app.state.config.cookie_name, ""),
            body["password"],
            request.client.host if request.client else "unknown",
            binding,
        )

    @routes.post("/auth/logout")
    def logout(request: Request):
        value = session(request)
        app.state.auth.revoke(value["id"])
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(
            app.state.config.cookie_name, path="/", secure=True, httponly=True, samesite="strict"
        )
        return response

    @routes.get("/auth/sessions")
    def sessions(request: Request):
        session(request)
        return {"results": app.state.auth.sessions()}

    @routes.post("/auth/sessions/{identity}/revoke")
    async def revoke(identity: str, request: Request):
        session(request)
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", identity):
            raise AuthError("Invalid browser session identity.", 422)
        body = await json_body(request, max_bytes=1024)
        if body:
            raise AuthError("Session revocation takes an empty operation object.", 422)
        admit_write(request, body)
        app.state.auth.revoke(identity)
        return {"revoked": True}

    @routes.post("/auth/revoke-all")
    async def revoke_all(request: Request):
        session(request)
        body = await json_body(request, max_bytes=1024)
        if body:
            raise AuthError("Session revocation takes an empty operation object.", 422)
        admit_write(request, body)
        app.state.auth.revoke()
        return {"revoked": True}

    return routes
