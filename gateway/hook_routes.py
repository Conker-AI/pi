"""The one gateway route outside sign-in: a webhook call that may start one AI job.

The caller proves nothing to the gateway but the job's secret, sent in the X-Conker-Hook-Token
header (never the address, which the server logs). The gateway checks its shape, caps the body,
and forwards it with its own runtime credential; Pi checks the secret, the job and the rate.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request

from .operations import parse_object
from .store import AuthError

JOB = re.compile(r"^job_[a-f0-9]{32}$")
TOKEN = re.compile(r"^hook_[A-Za-z0-9_-]{43}$")
MAX_BYTES = 16384


def router(app, forward):
    routes = APIRouter()

    @routes.post("/hooks/{job_id}", operation_id="webhook", status_code=202)
    async def hook(job_id: str, request: Request):
        token = request.headers.get("x-conker-hook-token", "")
        # One refusal for every bad shape, so a caller cannot probe which jobs exist.
        if not JOB.fullmatch(job_id) or not TOKEN.fullmatch(token):
            raise AuthError("Webhook refused.", 403)
        if request.scope["query_string"]:
            raise AuthError("Webhooks do not accept query parameters.", 422)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise AuthError("Send at most 16 KB to a webhook.", 413)
        kind = request.headers.get("content-type", "").split(";")[0].strip()
        body = (
            parse_object(raw)
            if raw and kind == "application/json"
            else {"text": raw.decode("utf-8", errors="replace")}
            if raw
            else {}
        )
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            forward,
            "POST",
            app.state.config.pi_url.rstrip("/") + f"/hooks/{job_id}",
            "X-Pi-Gateway-Key",
            app.state.config.pi_key,
            body,
            b"",
            {"X-Conker-Hook-Token": token},
        )

    return routes
