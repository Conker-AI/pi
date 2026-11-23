"""The owner turns notifications on or off for one device (docs/web-push.md).

Signed-in session, CSRF and the exact Origin on writes; no password (ADR-0010: a device that gets
content-free notifications can be turned off again, and sign-out everywhere turns off all of them).
Errors are fixed words: they never repeat the endpoint or the keys a browser sent.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import push
from .operations import json_body
from .store import AuthError

DEVICE = re.compile(r"[0-9a-f]{32}")


def _subscription(body: dict) -> tuple[str, str, str]:
    keys = body.get("keys")
    endpoint = body.get("endpoint")
    if (
        set(body) != {"endpoint", "keys"}
        or not isinstance(keys, dict)
        or set(keys) != {"p256dh", "auth"}
        or not isinstance(endpoint, str)
        or not all(isinstance(value, str) and len(value) <= 200 for value in keys.values())
        or not push.endpoint_allowed(endpoint)
    ):
        raise ValueError
    push.device_keys(keys["p256dh"], keys["auth"])
    return endpoint, keys["p256dh"], keys["auth"]


def router(app, session, admit_write):
    routes = APIRouter()

    def unavailable() -> AuthError:
        return AuthError(
            "Notifications are unavailable on this server. Check the gateway's data folder.", 503
        )

    def state(device: str | None = None) -> dict:
        devices = push.Devices(app.state.auth).ids()
        value = {"devices": [item["id"] for item in devices], "limit": push.MAX_DEVICES}
        return value if device is None else {"device": device, **value}

    @routes.get("/api/push")
    def read(request: Request):
        """The server's public key (made on first use) and the devices with notifications on."""
        session(request)
        if request.scope["query_string"]:
            raise AuthError("Notifications do not accept query parameters.", 422)
        try:
            key = push.server_key(Path(app.state.config.database).parent)
        except push.PushUnavailable:
            raise unavailable() from None
        return JSONResponse({"publicKey": push.public_key(key), **state()})

    @routes.post("/api/push/subscriptions")
    async def subscribe(request: Request):
        """Turn notifications on for the device this subscription names."""
        session(request)
        body = await json_body(request, max_bytes=4096)
        admit_write(request, body, password=False)
        try:
            endpoint, point, secret = _subscription(body)
        except (ValueError, TypeError):
            raise AuthError(
                "This browser's notification details were not accepted. Turn it off and on again.",
                422,
                code="push_subscription_invalid",
            ) from None
        return JSONResponse(state(push.Devices(app.state.auth).add(endpoint, point, secret)))

    @routes.post("/api/push/subscriptions/{device}/delete")
    async def unsubscribe(device: str, request: Request):
        """Turn notifications off for one device; turning off one that is already off is fine."""
        session(request)
        body = await json_body(request, max_bytes=64)
        admit_write(request, body, password=False)
        if body or not DEVICE.fullmatch(device):
            raise AuthError("Name one device with notifications on.", 422)
        removed = push.Devices(app.state.auth).remove(device)
        return JSONResponse({"removed": removed, **state()})

    return routes
