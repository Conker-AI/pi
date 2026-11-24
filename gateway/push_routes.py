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


def _subscription(body: dict) -> tuple[str, str, str, bool]:
    keys = body.get("keys")
    endpoint = body.get("endpoint")
    renew = body.get("renew", False)
    if (
        set(body) - {"renew"} != {"endpoint", "keys"}
        or type(renew) is not bool
        or not isinstance(keys, dict)
        or set(keys) != {"p256dh", "auth"}
        or not isinstance(endpoint, str)
        or not all(isinstance(value, str) and len(value) <= 200 for value in keys.values())
        or not push.endpoint_allowed(endpoint)
    ):
        raise ValueError
    push.device_keys(keys["p256dh"], keys["auth"])
    return endpoint, keys["p256dh"], keys["auth"], renew


def router(app, session, admit_write):
    routes = APIRouter()

    def unavailable() -> AuthError:
        return AuthError(
            "Notifications are unavailable on this server. Check the gateway's data folder.", 503
        )

    def state(device: str | None = None) -> dict:
        # Newest first, each with when it was turned on and its browser's short name, so the
        # owner can tell a device that is not theirs and remove it.
        value = {"devices": push.Devices(app.state.auth).ids(), "limit": push.MAX_DEVICES}
        return value if device is None else {"device": device, **value}

    def key():
        try:
            return push.server_key(Path(app.state.config.database).parent)
        except push.PushUnavailable:
            raise unavailable() from None

    @routes.get("/api/push")
    def read(request: Request):
        """The server's public key (made on first use) and the devices with notifications on."""
        session(request)
        if request.scope["query_string"]:
            raise AuthError("Notifications do not accept query parameters.", 422)
        return JSONResponse({"publicKey": push.public_key(key()), **state()})

    @routes.post("/api/push/subscriptions")
    async def subscribe(request: Request):
        """Turn notifications on for the device this subscription names, for this sign-in: signing
        this browser out turns it off. The other devices are told one more is on. With
        `"renew": true` (the browser after each sign-in) it only moves a device that is on to this
        sign-in, and a device that is off answers 404."""
        signed_in = session(request)
        body = await json_body(request, max_bytes=4096)
        admit_write(request, body, password=False)
        try:
            endpoint, point, secret, renew = _subscription(body)
        except (ValueError, TypeError):
            raise AuthError(
                "This browser's notification details were not accepted. Turn it off and on again.",
                422,
                code="push_subscription_invalid",
            ) from None
        label = push.device_label(request.headers.get("user-agent", ""))
        device, new = push.Devices(app.state.auth).add(
            endpoint,
            point,
            secret,
            session=signed_in["id"],
            key=push.public_key_id(key()),
            label=label,
            renew=renew,
        )
        if new:
            app.state.push.announce(device, label)
        return JSONResponse(state(device))

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
