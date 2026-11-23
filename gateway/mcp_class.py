"""The owner's read class for one imported MCP tool: Observe or Act outward (ToolGate ADR-0006).

Observe lets the tool run without its Inbox card, so it widens what Conker may do on his behalf:
like every access grant it needs his password, bound to this exact request (ADR-0010). Act
outward only makes the tool ask again, so the signed-in session is enough. ToolGate decides
whether the tool may be Observe (its names and its server's annotations); its 409 and 422
refusals come back as a known code with the gateway's own plain words, never ToolGate's text.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .mcp_control import TOOL_ID
from .operations import json_body
from .store import AuthError

OBSERVE, ACT_OUTWARD = "observe", "act_outward"
REFUSALS = {
    "act_outward_name": (
        "Its name could act, so it keeps asking every time. Only a tool named like get_weather "
        "or list_emails, and saying nothing that acts, can run without asking."
    ),
    "act_outward_hint": (
        "Its server does not say this tool is read-only, so it keeps asking every time."
    ),
    "schema_changed": (
        "Its server now advertises this tool differently, so it keeps asking every time. "
        "Import the reviewed new version first."
    ),
    "unsupported_schema": (
        "Its server's current input schema is not supported, so it keeps asking every time."
    ),
    "definition_conflict": "The tool changed since you opened it. Reload it and try again.",
}
REFUSED = "ToolGate refused the change; the tool kept its class. Reload it and try again."


def validate(body: dict) -> None:
    version = body.get("expected_version")
    if not (
        set(body) == {"read_class", "expected_version"}
        and body["read_class"] in (OBSERVE, ACT_OUTWARD)
        and type(version) is int
        and 1 <= version <= 2**53 - 1
    ):
        raise AuthError(
            "Send only read_class (observe or act_outward) and the expected_version you saw.", 422
        )


def plain(response: JSONResponse) -> JSONResponse:
    """ToolGate's 409/422 refusal as a known code and the gateway's own words."""
    try:
        detail = json.loads(bytes(response.body)).get("detail")
        code = str(detail.get("code", "")).lower() if isinstance(detail, dict) else ""
    except (ValueError, AttributeError):
        code = ""
    code = code if code in REFUSALS else "refused"
    return JSONResponse(
        {"detail": {"code": code, "message": REFUSALS.get(code, REFUSED)}},
        status_code=response.status_code,
    )


def router(app, session, forward, admit_write):
    routes = APIRouter()

    @routes.post("/api/owner/mcp/tools/{identity}/class")
    async def mcp_class(identity: str, request: Request):
        from starlette.concurrency import run_in_threadpool

        session(request)
        if request.scope["query_string"] or not re.fullmatch(TOOL_ID, identity):
            raise AuthError("Select a saved MCP tool without query parameters.", 422)
        body = await json_body(request)
        validate(body)
        # Observe skips the Inbox card for this tool: his password, as for every access grant.
        # Only Act outward, which makes it ask again, goes with the session alone.
        admit_write(request, body, password=body["read_class"] != ACT_OUTWARD)
        response = await run_in_threadpool(
            forward,
            "POST",
            app.state.config.toolgate_url.rstrip("/") + f"/v2/owner/mcp/tools/{identity}/class",
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            body,
            b"",
        )
        return plain(response) if response.status_code in (409, 422) else response

    return routes
