"""Browser access to Pi: the runtime and owner-control proxies, the answer stream and call audio."""

from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from pi.api.browser_contract import owner_allowed, runtime_allowed, session_only_write

from .operations import json_body
from .store import AuthError


def router(app, session, forward, admit_write, stream_transport):
    routes = APIRouter()

    @routes.get("/api/pi/turn-submissions/{request_id}/stream", operation_id="runtime_stream")
    async def runtime_stream(request_id: str, request: Request, after: int = Query(0, ge=0)):
        """Relay a live answer preview. Bytes pass through; upstream headers never do."""
        session(request)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
            raise AuthError("Invalid submission identity.", 422)
        url = f"{app.state.config.pi_url.rstrip('/')}/turn-submissions/{request_id}/stream"
        client = httpx.AsyncClient(
            transport=stream_transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(10.0, read=60.0),
        )
        try:
            upstream_request = client.build_request(
                "GET",
                url,
                headers={"X-Pi-Gateway-Key": app.state.config.pi_key},
                params={"after": after},
            )
            response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise AuthError(
                "Live preview unavailable; the saved turn is unaffected.", 503
            ) from None
        if response.status_code != 200 or not response.headers.get("content-type", "").startswith(
            "text/event-stream"
        ):
            await response.aclose()
            await client.aclose()
            raise AuthError("Live preview unavailable; the saved turn is unaffected.", 502)

        async def relay():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            except httpx.HTTPError:
                return
            finally:
                await response.aclose()
                await client.aclose()

        return StreamingResponse(
            relay(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @routes.get("/api/pi/{path:path}", operation_id="runtime_read")
    @routes.post("/api/pi/{path:path}", operation_id="runtime_write")
    async def runtime(path: str, request: Request):
        session(request)
        target = "/" + path
        if not runtime_allowed(request.method, target):
            raise AuthError("This operation is not available through the browser gateway.", 403)
        body = await json_body(request) if request.method == "POST" else None
        if body is not None:
            if session_only_write(request.method, target):
                # Signed-in session, CSRF and origin were checked above; see ADR-0010.
                if request.scope["query_string"]:
                    raise AuthError("Write operations must not include query parameters.", 422)
            else:
                admit_write(request, body)
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            forward,
            request.method,
            app.state.config.pi_url.rstrip("/") + target,
            "X-Pi-Gateway-Key",
            app.state.config.pi_key,
            body,
            request.scope["query_string"],
        )

    @routes.post("/api/control/pi/calls/browser/{identity}/audio")
    async def control_call_audio(identity: str, request: Request):
        """Relay one bounded transient WAV turn without converting media to JSON."""
        session(request)
        if not re.fullmatch(r"call_[a-f0-9]{32}", identity):
            raise AuthError("Invalid call identity.", 422)
        params = request.query_params
        request_id = params.get("request_id", "")
        if (
            set(params) != {"request_id"}
            or len(params.getlist("request_id")) != 1
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id)
        ):
            raise AuthError("Audio turns require exactly one valid request_id.", 422)
        if not app.state.config.pi_owner_key:
            raise AuthError("Pi owner-control connection is not configured.", 503)
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media not in {"audio/wav", "audio/wave", "audio/x-wav", "audio/vnd.wave"}:
            raise AuthError("Audio turns require PCM WAV audio.", 415)
        declared = request.headers.get("content-length")
        if declared:
            if not declared.isdecimal():
                raise AuthError("Invalid audio content length.", 400)
            if int(declared) > 10 * 1024 * 1024:
                raise AuthError("Audio exceeds 10 MiB.", 413)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 10 * 1024 * 1024:
                raise AuthError("Audio exceeds 10 MiB.", 413)
            body.extend(chunk)
        if not body:
            raise AuthError("Audio is empty.", 422)

        def relay_audio():
            try:
                outgoing = app.state.client.build_request(
                    "POST",
                    app.state.config.pi_url.rstrip("/") + f"/calls/browser/{identity}/audio",
                    headers={
                        "X-Pi-Owner-Key": app.state.config.pi_owner_key,
                        "Content-Type": "audio/wav",
                        "Accept": "application/json",
                    },
                    params={"request_id": request_id},
                    content=bytes(body),
                    timeout=660,
                )
                outgoing.headers.pop("cookie", None)
                response = app.state.client.send(outgoing)
            except (httpx.HTTPError, UnicodeError):
                raise AuthError(
                    "Speech service unavailable. The turn was not retried; check the saved call.",
                    503,
                ) from None
            if 300 <= response.status_code < 400:
                raise AuthError("Speech service returned an unexpected redirect.", 502)
            if len(response.content) > 32 * 1024 * 1024:
                raise AuthError("Speech response exceeded the browser boundary.", 502)
            try:
                data = response.json()
            except ValueError:
                raise AuthError("Speech service returned an unreadable response.", 502) from None
            return JSONResponse(data, status_code=response.status_code)

        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(relay_audio)

    @routes.get("/api/control/pi/{path:path}", operation_id="control_read")
    @routes.post("/api/control/pi/{path:path}", operation_id="control_write")
    async def control(path: str, request: Request):
        session(request)
        target = "/" + path
        if not owner_allowed(request.method, target):
            raise AuthError("This owner operation is not available through the gateway.", 403)
        if target.startswith("/memory/corrections") and request.scope["query_string"]:
            raise AuthError("Memory corrections do not accept query parameters.", 422)
        if not app.state.config.pi_owner_key:
            raise AuthError("Pi owner-control connection is not configured.", 503)
        body = (
            await json_body(
                request,
                max_bytes=66 * 1024 * 1024
                if target.startswith("/characters/companion/")
                else 128000
                if target.startswith("/memory/corrections")
                else 65536,
            )
            if request.method == "POST"
            else None
        )
        if body is not None:
            if session_only_write(request.method, target):
                if request.scope["query_string"]:
                    raise AuthError("Write operations must not include query parameters.", 422)
            else:
                admit_write(request, body)
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            forward,
            request.method,
            app.state.config.pi_url.rstrip("/") + target,
            "X-Pi-Owner-Key",
            app.state.config.pi_owner_key,
            body,
            request.scope["query_string"],
        )

    return routes
