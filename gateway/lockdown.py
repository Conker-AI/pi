"""ToolGate's lockdown comes off only with the owner's password: the Settings switch and the host
command `python -m gateway lockdown`.

In lockdown no tool or workflow runs and no agent files a request. Turning it on only takes power
away, so the signed-in session is enough. Turning it off gives the power back and asks for the
password (ADR-0010): in the browser a single-use proof bound to exactly `{"enabled": false}`
(`admit_write`), at the host the password itself, once, on stdin, never in argv or the environment
(`AuthStore.host_password`: five wrong per five minutes, shared with the other host commands).
ToolGate cannot check the password, so it lifts lockdown only when the owner key comes with
`PASSWORD_PROOF`, which these two paths send, each only after its check. `conker config set`
reaches the same ToolGate route through a relay in this container with the owner key and no
proof, and is refused (403 `password_required`); the CLI then sends the same change here with
the password. Nothing here repeats the password or the request in an error.
"""

from __future__ import annotations

import json
import sys
from typing import BinaryIO, TextIO

import httpx
from fastapi import APIRouter, Request

from .operations import json_body
from .store import AuthError, AuthStore

# The gateway's word to ToolGate that it checked the owner's password for this lockdown change.
# Not a secret: it keeps a command that never asked the password from lifting lockdown.
PASSWORD_PROOF = {"X-ToolGate-Owner-Proof": "password"}
MAX_REQUEST_BYTES = 4096
NOTHING = " Nothing was changed."
MAYBE = " It may already be off; check with conker config get access.lockdown before trying again."


def router(app, session, forward, admit_write):
    routes = APIRouter()

    @routes.get("/api/owner/lockdown")
    @routes.post("/api/owner/lockdown")
    async def lockdown(request: Request):
        """ToolGate's lockdown: the session turns it on; off asks for the password first."""
        from starlette.concurrency import run_in_threadpool

        session(request)
        if request.scope["query_string"]:
            raise AuthError("Lockdown does not accept query parameters.", 422)
        body = await json_body(request, max_bytes=1024) if request.method == "POST" else None
        checked = False
        if body is not None:
            if set(body) != {"enabled"} or type(body["enabled"]) is not bool:
                raise AuthError("Send only enabled: true or false.", 422)
            if not body["enabled"]:
                admit_write(request, body)
                checked = True
        return await run_in_threadpool(
            forward,
            request.method,
            app.state.config.toolgate_url.rstrip("/") + "/v2/owner/lockdown",
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            body,
            b"",
            PASSWORD_PROOF if checked else None,
        )

    return routes


class NotChanged(ValueError):
    pass


def read_request(stream: BinaryIO) -> tuple[str, bool]:
    """The password and the switch (`enabled`), checked before any use."""
    raw = stream.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise NotChanged("The request is too large." + NOTHING)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NotChanged("Send the password and the switch as one JSON object." + NOTHING) from None
    if (
        not isinstance(value, dict)
        or set(value) != {"password", "enabled"}
        or not isinstance(value["password"], str)
        or not value["password"]
        or type(value["enabled"]) is not bool
    ):
        raise NotChanged("Send a password and enabled: true or false." + NOTHING)
    return value["password"], value["enabled"]


def switch(config, client: httpx.Client, enabled: bool) -> dict:
    """ToolGate's lockdown with the owner key and the proof; its view once it confirms."""
    try:
        response = client.post(
            config.toolgate_url.rstrip("/") + "/v2/owner/lockdown",
            headers={
                "Accept": "application/json",
                "X-ToolGate-Owner-Key": config.owner_key,
                **PASSWORD_PROOF,
            },
            json={"enabled": enabled},
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise NotChanged("ToolGate did not answer." + NOTHING) from None
    except (httpx.HTTPError, OSError):
        raise NotChanged("ToolGate gave no complete answer." + MAYBE) from None
    if response.status_code != 200:
        raise NotChanged(f"ToolGate refused it (HTTP {response.status_code})." + NOTHING)
    try:
        view = response.json()
    except ValueError:
        view = None
    if not isinstance(view, dict) or type(view.get("lockdown")) is not bool:
        raise NotChanged("ToolGate answered with something unreadable." + MAYBE)
    if view["lockdown"] is not enabled:
        raise NotChanged("ToolGate did not confirm the change. Check with conker config get.")
    return view


def run(config, auth: AuthStore, client: httpx.Client, stdin: BinaryIO, out: TextIO, err: TextIO):
    try:
        password, enabled = read_request(stdin)
        if not config.owner_key:
            raise NotChanged("The ToolGate owner channel is not configured." + NOTHING)
        try:
            auth.host_password(password)
        except AuthError as exc:
            raise NotChanged(str(exc) + NOTHING) from None
        view = switch(config, client, enabled)
    except NotChanged as exc:
        print(f"Gateway did not complete: {exc}", file=err)
        return 1
    print(json.dumps(view, sort_keys=True), file=out)
    return 0


def main(db: str) -> int:
    from .api import Config

    config = Config.environment()
    config.validate()
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=30) as client:
        return run(config, AuthStore(db), client, sys.stdin.buffer, sys.stdout, sys.stderr)
