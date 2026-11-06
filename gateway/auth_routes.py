"""Owner sign-in: session, login, activity, verification, logout, revocation and wrong-password alerts."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .operations import fingerprint, json_body
from .store import DEVICE_SECONDS, AuthError


def device_cookie(session_cookie: str) -> str:
    """The known-browser cookie lives under /auth only, which a __Host- name forbids."""
    return "__Secure-" + session_cookie.removeprefix("__Host-") + "-device"


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

        device = device_cookie(app.state.config.cookie_name)
        value = await run_in_threadpool(
            app.state.auth.login,
            request.cookies.get(app.state.config.cookie_name, ""),
            body["password"],
            request.client.host if request.client else "unknown",
            request.cookies.get(device, ""),
        )
        response = cookie(
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
        # This browser signed in, so later sign-ins from it get their own attempt budget.
        response.set_cookie(
            device,
            value["device"],
            max_age=DEVICE_SECONDS,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/auth",
        )
        return response

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

    @routes.post("/auth/activity")
    async def activity(request: Request):
        """The page saw the owner's click or key: keep the sign-in unlocked for another window.

        The page sends it at most about once a minute and only on the owner's own input, so
        polling and an open but untouched tab never keep a session alive (L-03). An expired
        sign-in stays expired.
        """
        session(request)
        body = await json_body(request, max_bytes=64)
        if body or request.scope["query_string"]:
            raise AuthError("Activity takes an empty operation object.", 422)
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            app.state.auth.activity, request.cookies.get(app.state.config.cookie_name, "")
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
        # Signing out only takes access away.
        admit_write(request, body, password=False)
        app.state.auth.revoke(identity)
        return {"revoked": True}

    @routes.post("/auth/revoke-all")
    async def revoke_all(request: Request):
        session(request)
        body = await json_body(request, max_bytes=1024)
        if body:
            raise AuthError("Session revocation takes an empty operation object.", 422)
        # Signing out only takes access away.
        admit_write(request, body, password=False)
        app.state.auth.revoke()
        return {"revoked": True}

    @routes.get("/auth/alerts")
    def alerts(request: Request):
        session(request)
        return app.state.auth.alerts()

    @routes.post("/auth/alerts/seen")
    async def alerts_seen(request: Request):
        session(request)
        if request.scope["query_string"] or await json_body(request, max_bytes=1024):
            raise AuthError("Marking alerts seen takes an empty JSON object.", 422)
        app.state.auth.alerts_seen()
        return {"seen": True}

    return routes
