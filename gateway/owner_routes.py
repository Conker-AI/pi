"""Owner tools through ToolGate: MCP tools, editor drafts, their runs and publications, and approval requests."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request

from .answers import continue_after
from .operations import json_body
from .store import AuthError

ACCESS_TOOL_ID = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
# The Inbox button templates ToolGate knows (toolgate/core/ask_cards.py).
CARD_TEMPLATES = ("do", "send", "publish", "ship", "run", "delete", "pay", "save")


def router(app, session, forward, admit_write):
    routes = APIRouter()

    @routes.get("/api/owner/mcp/tools")
    @routes.get("/api/owner/mcp/tools/{identity}/access")
    @routes.post("/api/owner/mcp/discover")
    @routes.post("/api/owner/mcp/import")
    @routes.post("/api/owner/mcp/tools/{identity}/state")
    @routes.post("/api/owner/mcp/tools/{identity}/access")
    async def mcp_control(request: Request, identity: str | None = None):
        from starlette.concurrency import run_in_threadpool

        from .mcp_control import TOOL_ID, validate_body

        session(request)
        if request.scope["query_string"] or (
            identity is not None and not re.fullmatch(TOOL_ID, identity)
        ):
            raise AuthError("Select a saved MCP tool without query parameters.", 422)
        operation = request.url.path.rsplit("/", 1)[-1]
        extra = None
        if operation == "access":
            key = app.state.config.toolgate_execution_key
            if not key:
                raise AuthError("The scoped chat execution credential is not configured.", 503)
            extra = {"X-ToolGate-Execution-Key": key}
        body = await json_body(request) if request.method == "POST" else None
        if body is not None:
            validate_body(operation, body)
            admit_write(request, body)
        return await run_in_threadpool(
            forward,
            request.method,
            app.state.config.toolgate_url.rstrip("/")
            + request.url.path.replace("/api/owner/mcp/", "/v2/owner/mcp/", 1),
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            body,
            b"",
            extra,
        )

    @routes.get("/api/owner/access")
    @routes.post("/api/owner/access/delegate")
    @routes.post("/api/owner/tools/{identity}/sensitive")
    @routes.post("/api/owner/tools/{identity}/card")
    @routes.get("/api/owner/tools/{identity}/access")
    @routes.post("/api/owner/tools/{identity}/access")
    async def access_control(request: Request, identity: str | None = None):
        """Whether Conker decides by his access settings, the tools that always ask him, the
        buttons his Inbox shows for a tool, and which tools Conker may use at all."""
        from starlette.concurrency import run_in_threadpool

        from .mcp_control import validate_body

        session(request)
        tool_access = identity is not None and request.url.path.endswith("/access")
        card = identity is not None and request.url.path.endswith("/card")
        if request.scope["query_string"] or (
            identity is not None and not re.fullmatch(ACCESS_TOOL_ID, identity)
        ):
            raise AuthError("Select a tool without query parameters.", 422)
        body = await json_body(request) if request.method == "POST" else None
        if body is not None:
            if tool_access:
                validate_body("access", body)
            elif card:
                if set(body) != {"template"} or body["template"] not in CARD_TEMPLATES:
                    raise AuthError("Send only template: one of " + ", ".join(CARD_TEMPLATES), 422)
            else:
                field = "sensitive" if identity is not None else "enabled"
                if set(body) != {field} or not isinstance(body[field], bool):
                    raise AuthError(f"Send only {field}: true or false.", 422)
            admit_write(request, body)
        extra = None
        if identity is None or tool_access:
            # The delegate and the tool grants are the chat's own scoped key; the owner key alone
            # cannot name it.
            key = app.state.config.toolgate_execution_key
            if not key:
                raise AuthError("The scoped chat execution credential is not configured.", 503)
            extra = {"X-ToolGate-Execution-Key": key}
        return await run_in_threadpool(
            forward,
            request.method,
            app.state.config.toolgate_url.rstrip("/")
            + request.url.path.replace("/api/owner/", "/v2/owner/", 1),
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            body,
            b"",
            extra,
        )

    @routes.get("/api/owner/editor-capabilities")
    def editor_capabilities(request: Request):
        session(request)
        params = request.query_params
        if set(params) - {"kind", "q", "after", "limit"} or any(
            len(params.getlist(key)) != 1 for key in params
        ):
            raise AuthError("Use only capability search parameters.", 422)
        if (
            params.get("kind", "tool") not in {"tool", "workflow"}
            or len(params.get("q", "")) > 100
            or (
                "after" in params and not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,79}", params["after"])
            )
            or (
                "limit" in params
                and (
                    not re.fullmatch(r"[0-9]{1,3}", params["limit"])
                    or not 1 <= int(params["limit"]) <= 100
                )
            )
        ):
            raise AuthError("Invalid capability search parameters.", 422)
        return forward(
            "GET",
            app.state.config.toolgate_url.rstrip("/") + "/v2/owner/editor-capabilities",
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            None,
            request.scope["query_string"],
        )

    @routes.get("/api/owner/editor-drafts")
    def editor_drafts(request: Request):
        session(request)
        params = request.query_params
        if set(params) - {"limit", "after"} or any(len(params.getlist(key)) != 1 for key in params):
            raise AuthError("Use only editor draft pagination parameters.", 422)
        limit, after = params.get("limit"), params.get("after")
        if (
            limit is not None
            and (not re.fullmatch(r"[0-9]{1,3}", limit) or not 1 <= int(limit) <= 100)
        ) or (after is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", after)):
            raise AuthError("Invalid editor draft pagination.", 422)
        return forward(
            "GET",
            app.state.config.toolgate_url.rstrip("/") + "/v2/owner/editor-drafts",
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            None,
            request.scope["query_string"],
        )

    @routes.get("/api/owner/editor-drafts/{identity}")
    @routes.post("/api/owner/editor-drafts/{identity}")
    async def editor_draft(identity: str, request: Request):
        session(request)
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", identity)
            or request.scope["query_string"]
        ):
            raise AuthError("Invalid editor draft identity.", 422)
        body = await json_body(request) if request.method == "POST" else None
        if body is not None:
            if (
                set(body) != {"expected_revision", "document"}
                or type(body.get("expected_revision")) is not int
                or body["expected_revision"] < 0
                or not isinstance(body.get("document"), dict)
                or body["document"].get("id") != identity
            ):
                raise AuthError("Send an editor document and its expected revision.", 422)
            admit_write(request, body)
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            forward,
            request.method,
            app.state.config.toolgate_url.rstrip("/") + f"/v2/owner/editor-drafts/{identity}",
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            body,
            b"",
        )

    @routes.get("/api/owner/editor-drafts/{identity}/access")
    @routes.post("/api/owner/editor-drafts/{identity}/access")
    @routes.get("/api/owner/editor-drafts/{identity}/runs")
    @routes.post("/api/owner/editor-drafts/{identity}/runs")
    async def editor_execution(identity: str, request: Request):
        session(request)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", identity):
            raise AuthError("Invalid editor draft identity.", 422)
        key = app.state.config.toolgate_execution_key
        if not key:
            raise AuthError(
                "The scoped workflow execution credential is not configured on this host.", 503
            )
        operation = request.url.path.rsplit("/", 1)[-1]
        body = await json_body(request) if request.method == "POST" else None
        params = request.query_params
        if body is not None:
            required = (
                {"version", "digest", "enabled"}
                if operation == "access"
                else {"version", "digest", "action_id", "args"}
            )
            optional = set() if operation == "access" else {"approval_request_id"}
            if (
                not required <= body.keys()
                or body.keys() - required - optional
                or type(body.get("version")) is not int
                or body["version"] < 1
                or not isinstance(body.get("digest"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", body["digest"])
                or (operation == "access" and type(body.get("enabled")) is not bool)
                or (
                    operation == "runs"
                    and (
                        not isinstance(body.get("action_id"), str)
                        or not re.fullmatch(r"editor_[a-f0-9]{32}", body["action_id"])
                        or not isinstance(body.get("args"), dict)
                    )
                )
            ):
                raise AuthError(
                    "Supply the exact workflow version and bounded operation fields.", 422
                )
            admit_write(request, body)
        elif operation == "access":
            if (
                set(params) != {"version", "digest"}
                or any(len(params.getlist(k)) != 1 for k in params)
                or not re.fullmatch(r"[1-9][0-9]{0,8}", params["version"])
                or not re.fullmatch(r"[a-f0-9]{64}", params["digest"])
            ):
                raise AuthError("Select the exact published workflow version.", 422)
        elif params:
            raise AuthError("Run history accepts no query parameters.", 422)
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            forward,
            request.method,
            app.state.config.toolgate_url.rstrip("/")
            + f"/v2/owner/editor-drafts/{identity}/{operation}",
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            body,
            request.scope["query_string"],
            {"X-ToolGate-Execution-Key": key},
        )

    @routes.get("/api/owner/editor-drafts/{identity}/publications")
    @routes.get("/api/owner/editor-drafts/{identity}/validation")
    @routes.post("/api/owner/editor-drafts/{identity}/publish")
    async def editor_publication(identity: str, request: Request):
        session(request)
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", identity)
            or request.scope["query_string"]
        ):
            raise AuthError("Invalid editor draft identity.", 422)
        operation = request.url.path.rsplit("/", 1)[-1]
        body = await json_body(request) if request.method == "POST" else None
        if body is not None:
            if (
                set(body) != {"expected_revision", "expected_publication_version", "authorization"}
                or type(body.get("expected_revision")) is not int
                or body["expected_revision"] < 1
                or type(body.get("expected_publication_version")) is not int
                or body["expected_publication_version"] < 0
                or body.get("authorization") not in {"auto", "owner_confirmation"}
            ):
                raise AuthError(
                    "Select the saved draft, publication version and authorization.", 422
                )
            admit_write(request, body)
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            forward,
            request.method,
            app.state.config.toolgate_url.rstrip("/")
            + f"/v2/owner/editor-drafts/{identity}/{operation}",
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            body,
            b"",
        )

    @routes.get("/api/owner/requests")
    def owner_requests(request: Request):
        session(request)
        # Only the bounded owner-list pagination contract crosses this channel.
        if set(request.query_params) - {"limit", "cursor"} or any(
            len(request.query_params.getlist(key)) != 1 for key in request.query_params
        ):
            raise AuthError("Use only owner request pagination parameters.", 422)
        limit, cursor = request.query_params.get("limit"), request.query_params.get("cursor")
        if (
            limit is not None
            and (not re.fullmatch(r"[0-9]{1,3}", limit) or not 1 <= int(limit) <= 200)
        ) or (cursor is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cursor)):
            raise AuthError("Use valid owner request pagination parameters.", 422)
        return forward(
            "GET",
            app.state.config.toolgate_url.rstrip("/") + "/v2/owner/requests",
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            None,
            request.scope["query_string"],
        )

    @routes.get("/api/owner/requests/{identity}")
    def owner_request(identity: str, request: Request):
        session(request)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity) or request.scope["query_string"]:
            raise AuthError("Invalid owner request identity.", 422)
        return forward(
            "GET",
            app.state.config.toolgate_url.rstrip("/") + f"/v2/owner/requests/{identity}",
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            None,
            b"",
        )

    @routes.post("/api/owner/requests/{identity}/decision")
    async def owner_decision(identity: str, request: Request):
        session(request)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
            raise AuthError("Invalid request identifier. Reload the approval list.", 422)
        body = await json_body(request)
        if (
            set(body) - {"status", "note"}
            or body.get("status") not in {"approved", "rejected", "dismissed"}
            or not isinstance(body.get("note", ""), str)
            or len(body.get("note", "")) > 2000
        ):
            raise AuthError("Send a decision status and optional note.", 422)
        from starlette.concurrency import run_in_threadpool

        admit_write(request, body)
        decided = await run_in_threadpool(
            forward,
            "POST",
            app.state.config.toolgate_url.rstrip("/") + f"/v2/owner/requests/{identity}/decision",
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            body,
            b"",
        )
        if decided.status_code == 200:
            # His decision continues the work: Do it runs now; a no closes and passes his words.
            await run_in_threadpool(
                continue_after, app.state.client, app.state.config, identity, body
            )
        return decided

    return routes
