"""Owner host controls: provider credentials, ChatGPT and Claude sign-in, native Claude and Codex sessions."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .operations import json_body
from .store import AuthError


def router(app, session, admit_write):
    routes = APIRouter()

    @routes.get("/api/host/providers")
    @routes.post("/api/host/providers")
    async def host_providers(request: Request):
        from starlette.concurrency import run_in_threadpool

        from . import provider_control

        session(request)
        if request.scope["query_string"]:
            raise AuthError("Provider control does not accept query parameters.", 422)
        body = None
        if request.method == "POST":
            try:
                body = provider_control.validate_operation(
                    await json_body(request, max_bytes=16384)
                )
            except provider_control.ProviderControlError:
                raise AuthError("Invalid provider operation.", 422) from None
            admit_write(request, body)
        try:
            value = await run_in_threadpool(
                provider_control.request,
                app.state.config.provider_control_socket,
                request.method,
                body,
                transport=app.state.provider_transport
                if app.state.config.provider_control_socket
                else None,
            )
        except provider_control.ProviderControlError as error:
            raise AuthError(str(error), 503) from None
        return JSONResponse(value)

    @routes.get("/api/host/chatgpt")
    @routes.post("/api/host/chatgpt")
    async def host_chatgpt(request: Request):
        from starlette.concurrency import run_in_threadpool

        from . import chatgpt
        from .provider_control import ProviderControlError

        session(request)
        if request.scope["query_string"]:
            raise AuthError("Subscription control does not accept query parameters.", 422)
        body = None
        if request.method == "POST":
            try:
                body = chatgpt.validate_operation(await json_body(request, max_bytes=1024))
            except ProviderControlError:
                raise AuthError("Invalid subscription operation.", 422) from None
            admit_write(request, body)
        try:
            value = await run_in_threadpool(
                chatgpt.request,
                app.state.config.provider_control_socket,
                request.method,
                body,
                transport=app.state.provider_transport,
            )
        except ProviderControlError as error:
            raise AuthError(str(error), 503) from None
        return JSONResponse(value)

    @routes.get("/api/host/claude")
    @routes.post("/api/host/claude")
    async def host_claude(request: Request):
        from starlette.concurrency import run_in_threadpool

        from . import native_claude
        from .provider_control import ProviderControlError

        session(request)
        if request.scope["query_string"]:
            raise AuthError("Native control does not accept query parameters.", 422)
        body = None
        if request.method == "POST":
            try:
                body = native_claude.Operation.model_validate(
                    await json_body(request, max_bytes=1024)
                ).model_dump(exclude_none=True)
                if body["operation"] != "login" and "billing" in body:
                    raise ValueError()
            except ValueError:
                raise AuthError("Invalid native authentication operation.", 422) from None
            admit_write(request, body)
        try:
            return JSONResponse(
                await run_in_threadpool(
                    native_claude.request, app.state.provider_transport, request.method, body
                )
            )
        except ProviderControlError as exc:
            raise AuthError(str(exc), 503) from None

    async def native_session_request(request: Request, action: str, identity: str | None = None):
        from starlette.concurrency import run_in_threadpool

        from . import native_sessions

        session(request)
        if request.scope["query_string"] or (
            identity is not None and not re.fullmatch(native_sessions.SESSION_ID, identity)
        ):
            raise AuthError(
                "Invalid native session request.", 422, code="invalid_native_session_request"
            )
        body = None
        if action == "stop":
            try:
                body = native_sessions.Stop.model_validate(
                    await json_body(request, max_bytes=1024)
                ).model_dump()
            except ValueError:
                raise AuthError(
                    "Invalid native session stop operation.",
                    422,
                    code="invalid_native_session_request",
                ) from None
            admit_write(request, body)
        try:
            result = await run_in_threadpool(
                native_sessions.request, app.state.provider_transport, action, identity, body
            )
        except native_sessions.NativeSessionsError as exc:
            raise AuthError(str(exc), exc.status, code=exc.code) from None
        return JSONResponse(result)

    @routes.get("/api/host/claude/sessions")
    async def native_session_list(request: Request):
        return await native_session_request(request, "list")

    @routes.get("/api/host/claude/sessions/{identity}")
    async def native_session_status(identity: str, request: Request):
        return await native_session_request(request, "status", identity)

    @routes.get("/api/host/claude/sessions/{identity}/output")
    async def native_session_output(identity: str, request: Request):
        return await native_session_request(request, "output", identity)

    @routes.post("/api/host/claude/sessions/{identity}/stop")
    async def native_session_stop(identity: str, request: Request):
        return await native_session_request(request, "stop", identity)

    async def codex_session_request(request: Request, action: str, identity: str | None = None):
        from starlette.concurrency import run_in_threadpool

        from . import codex_sessions
        from .native_sessions import NativeSessionsError

        session(request)
        try:
            result = await run_in_threadpool(
                codex_sessions.request,
                app.state.provider_transport,
                action,
                identity,
                request.scope["query_string"],
            )
        except NativeSessionsError as exc:
            raise AuthError(str(exc), exc.status, code=exc.code) from None
        return JSONResponse(result)

    @routes.get("/api/host/codex/sessions")
    async def codex_session_list(request: Request):
        return await codex_session_request(request, "list")

    @routes.get("/api/host/codex/sessions/{identity}")
    async def codex_session_status(identity: str, request: Request):
        return await codex_session_request(request, "status", identity)

    return routes
