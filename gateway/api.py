"""Same-origin browser authentication and an explicit service-operation allowlist."""

from __future__ import annotations

import base64
import hmac
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from pi.browser_contract import runtime_allowed, owner_allowed
from pi.owner_terminal import Terminal, TerminalError

from .dashboard import API_CSP, UI_CSP, DashboardAssets
from .operations import fingerprint, parse_object
from .store import AuthError, AuthStore
from .terminals import Terminals

COOKIE = "__Host-conker"


@dataclass(frozen=True)
class Config:
    origin: str
    database: str
    pi_url: str
    pi_key: str
    toolgate_url: str = "http://toolgate-api:8010"
    owner_key: str = ""
    dashboard_dir: str = ""
    idle_timeout_seconds: int = 1800
    terminal_shell: str = ""
    terminal_directory: str = ""
    pi_owner_key: str = ""

    def validate(self) -> None:
        if self.pi_owner_key and (len(self.pi_owner_key) < 32 or self.pi_owner_key in {self.pi_key, self.owner_key}):
            raise ValueError("Provision a distinct Pi owner-control key of at least 32 characters.")
        if bool(self.terminal_shell) != bool(self.terminal_directory):
            raise ValueError("Configure terminal shell and directory together.")
        if self.terminal_shell and (
            not os.path.isabs(self.terminal_shell) or not os.path.isabs(self.terminal_directory)
        ):
            raise ValueError("Terminal configuration requires absolute paths.")
        if (
            type(self.idle_timeout_seconds) is not int
            or not 60 <= self.idle_timeout_seconds <= 86400
        ):
            raise ValueError("GATEWAY_IDLE_TIMEOUT_SECONDS must be between 60 and 86400.")
        origin = urlsplit(self.origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError(
                "Set GATEWAY_ORIGIN to the exact HTTPS browser origin, with no trailing slash."
            )
        if len(self.pi_key) < 32 or self.owner_key == self.pi_key:
            raise ValueError(
                "Provision a distinct PI_GATEWAY_KEY of at least 32 characters on the host."
            )
        for value in (self.pi_url, self.toolgate_url):
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.path not in {"", "/"}
                or url.query
                or url.fragment
            ):
                raise ValueError(
                    "Set gateway service URLs to fixed origins without credentials or paths."
                )

    @classmethod
    def environment(cls) -> Config:
        return cls(
            os.environ.get("GATEWAY_ORIGIN", ""),
            os.environ.get("GATEWAY_DB_PATH", "/auth/auth.db"),
            os.environ.get("GATEWAY_PI_URL", "http://pi:8050"),
            os.environ.get("PI_GATEWAY_KEY", ""),
            os.environ.get("GATEWAY_TOOLGATE_URL", "http://toolgate-api:8010"),
            os.environ.get("GATEWAY_TOOLGATE_OWNER_KEY", ""),
            os.environ.get("GATEWAY_DASHBOARD_DIR", ""),
            int(os.environ.get("GATEWAY_IDLE_TIMEOUT_SECONDS", "1800")),
            os.environ.get("GATEWAY_TERMINAL_SHELL", ""),
            os.environ.get("GATEWAY_TERMINAL_DIRECTORY", ""),
            os.environ.get("GATEWAY_PI_OWNER_KEY", ""),
        )


def create_app(
    config: Config | None = None,
    *,
    store: AuthStore | None = None,
    transport: httpx.BaseTransport | None = None,
    terminal_factory=Terminal,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = config or Config.environment()
        settings.validate()
        app.state.config = settings
        app.state.dashboard = (
            DashboardAssets(settings.dashboard_dir) if settings.dashboard_dir else None
        )
        app.state.auth = store or AuthStore(
            settings.database, idle_seconds=settings.idle_timeout_seconds
        )
        app.state.terminals = (
            Terminals(
                settings.terminal_shell, settings.terminal_directory, factory=terminal_factory
            )
            if settings.terminal_shell
            else None
        )
        with httpx.Client(
            transport=transport, timeout=660, follow_redirects=False, trust_env=False
        ) as client:
            app.state.client = client
            try:
                yield
            finally:
                if app.state.terminals:
                    app.state.terminals.close()

    app = FastAPI(
        title="Conker browser gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError):
        detail = {"code": exc.code, "message": str(exc)} if exc.code else str(exc)
        return JSONResponse({"detail": detail}, status_code=exc.status)

    @app.exception_handler(TerminalError)
    async def terminal_error(request: Request, exc: TerminalError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        settings = app.state.config
        if (
            request.url.scheme != "https"
            or request.headers.get("host") != urlsplit(settings.origin).netloc
        ):
            return JSONResponse(
                {"detail": "Use the configured HTTPS address to open Conker."}, status_code=400
            )
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": (
                    UI_CSP if getattr(request.state, "dashboard_response", False) else API_CSP
                ),
            }
        )
        return response

    def session(request: Request, *, authenticated: bool = True) -> dict:
        auth = app.state.auth
        token = request.cookies.get(COOKIE, "")
        value = auth.session(token, authenticated=authenticated)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:  # noqa: SIM102 - group unsafe-method checks
            if request.headers.get("origin") != app.state.config.origin or not hmac.compare_digest(
                request.headers.get("x-csrf-token", "").encode(), value["csrf"].encode()
            ):
                raise AuthError("Request verification failed. Reload Conker and try again.", 403)
        return value

    def cookie(response: JSONResponse, token: str) -> JSONResponse:
        response.set_cookie(COOKIE, token, secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/health")
    def health():
        checks = {
            "owner_login": {
                "status": "ok" if app.state.auth.configured() else "degraded",
                "reason": "Password configured"
                if app.state.auth.configured()
                else "Run conker auth setup on the host",
            }
        }
        for name, url, header, key in (
            (
                "runtime",
                app.state.config.pi_url.rstrip("/") + "/health",
                "X-Pi-Gateway-Key",
                app.state.config.pi_key,
            ),
            (
                "owner_channel",
                app.state.config.toolgate_url.rstrip("/") + "/v2/owner/requests",
                "X-ToolGate-Owner-Key",
                app.state.config.owner_key,
            ),
        ):
            if not key:
                checks[name] = {
                    "status": "not_configured",
                    "reason": "Provision the owner channel on the host",
                }
                continue
            try:
                result = upstream("GET", url, header, key, None, b"", timeout=3)
                value = result.json()
                status = value.get("status", "unknown") if name == "runtime" else "ok"
                checks[name] = {"status": status if result.status_code == 200 else "unavailable"}
            except (httpx.HTTPError, ValueError, AttributeError):
                checks[name] = {"status": "unavailable"}
        degraded = sorted(
            name
            for name, value in checks.items()
            if value["status"] not in {"ok", "not_configured"}
        )
        return {
            "service": "gateway",
            "version": "0.1.0",
            "status": "degraded" if degraded else "ok",
            "checks": checks,
            "degraded": degraded,
            "checked_at": datetime.now(UTC).isoformat(),
            "age_seconds": 0.0,
        }

    @app.get("/auth/session")
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

    @app.post("/auth/login")
    async def login(request: Request):
        session(request, authenticated=False)
        body = await json_body(request)
        if set(body) != {"password"} or not isinstance(body["password"], str):
            raise AuthError("Send a password in a JSON object.", 422)
        # FastAPI runs sync routes in a threadpool; do the expensive check there too.
        from starlette.concurrency import run_in_threadpool

        value = await run_in_threadpool(
            app.state.auth.login,
            request.cookies.get(COOKIE, ""),
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

    @app.post("/auth/verify")
    async def verify(request: Request):
        session(request)
        body = await json_body(request)
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
            request.cookies.get(COOKIE, ""),
            body["password"],
            request.client.host if request.client else "unknown",
            binding,
        )

    @app.post("/auth/logout")
    def logout(request: Request):
        value = session(request)
        app.state.auth.revoke(value["id"])
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @app.get("/auth/sessions")
    def sessions(request: Request):
        session(request)
        return {"results": app.state.auth.sessions()}

    @app.post("/auth/sessions/{identity}/revoke")
    def revoke(identity: str, request: Request):
        session(request)
        app.state.auth.revoke(identity)
        return {"revoked": True}

    @app.post("/auth/revoke-all")
    def revoke_all(request: Request):
        session(request)
        app.state.auth.revoke()
        return {"revoked": True}

    def upstream(
        method: str,
        url: str,
        key_header: str,
        key: str,
        body: dict | None,
        query: bytes,
        *,
        timeout: float = 660,
    ) -> httpx.Response:
        outgoing = app.state.client.build_request(
            method,
            url,
            headers={key_header: key},
            json=body,
            params=query.decode("ascii") if query else None,
            timeout=timeout,
        )
        # A shared HTTP client must not turn a service's Set-Cookie into later authority.
        outgoing.headers.pop("cookie", None)
        return app.state.client.send(outgoing)

    def forward(method: str, url: str, key_header: str, key: str, body: dict | None, query: bytes):
        if not key:
            raise AuthError(
                "Owner approval channel is not configured. "
                "Provision its scoped credential on the host.",
                503,
            )
        try:
            response = upstream(method, url, key_header, key, body, query)
        except (httpx.HTTPError, UnicodeError):
            raise AuthError(
                "Service unavailable; check conker status. The operation was not retried; "
                "check its state before repeating it.",
                503,
            ) from None
        if 300 <= response.status_code < 400:
            raise AuthError(
                "Service returned an unexpected redirect. Check gateway service URLs on the host.",
                502,
            )
        try:
            data = response.json()
        except ValueError:
            raise AuthError(
                "Service returned an unreadable response. Check its logs before retrying.", 502
            ) from None
        # Upstream headers (especially cookies) never cross the browser boundary.
        return JSONResponse(data, status_code=response.status_code)

    def admit_write(request: Request, body: dict) -> None:
        if request.scope["query_string"]:
            raise AuthError("Write operations must not include query parameters.", 422)
        binding = fingerprint(request.method, request.url.path, body)
        app.state.auth.consume(
            request.cookies.get(COOKIE, ""),
            request.headers.get("x-conker-verification", ""),
            binding,
        )

    def terminal_access(request):
        owner = session(request)
        if app.state.terminals is None:
            raise AuthError("Owner terminal is not configured.", 503)
        return owner, app.state.terminals

    @app.post("/api/terminal")
    async def terminal_create(request: Request):
        owner, manager = terminal_access(request)
        body = await json_body(request)
        if set(body) != {"requestId"} or request.scope["query_string"]:
            raise AuthError("Supply only a terminal requestId.", 422)
        token = request.cookies.get(COOKIE, "")

        def validate():
            current = app.state.auth.session(token)
            if current["id"] != owner["id"]:
                raise AuthError("Terminal session identity changed.")

        return manager.create(
            owner["id"], body["requestId"], validate, lambda: admit_write(request, body)
        )

    @app.get("/api/terminal/{identity}")
    def terminal_read(identity: str, request: Request, cursor: int = Query(default=0, ge=0)):
        owner, manager = terminal_access(request)
        result = manager.use(owner["id"], identity, "read", cursor)
        return {
            **result,
            "data": base64.b64encode(result["data"]).decode("ascii"),
            "encoding": "base64",
        }

    @app.post("/api/terminal/{identity}/{operation}")
    async def terminal_write(identity: str, operation: str, request: Request):
        owner, manager = terminal_access(request)
        body = await json_body(request)
        if request.scope["query_string"]:
            raise AuthError("Terminal writes cannot include query parameters.", 422)
        if operation == "input" and set(body) == {"data"} and isinstance(body["data"], str):
            try:
                data = base64.b64decode(body["data"], validate=True)
            except ValueError:
                raise AuthError("Use base64 terminal input.", 422) from None
            return {
                "acceptedBytes": manager.use(owner["id"], identity, "write", data),
                "automaticReplay": False,
            }
        if operation == "resize" and set(body) == {"rows", "columns"}:
            manager.use(owner["id"], identity, "resize", body["rows"], body["columns"])
            return {"resized": True}
        if operation == "close" and not body:
            manager.use(owner["id"], identity, "close")
            return {"closed": True}
        raise AuthError("Invalid terminal operation.", 422)

    @app.get("/api/pi/{path:path}", operation_id="runtime_read")
    @app.post("/api/pi/{path:path}", operation_id="runtime_write")
    async def runtime(path: str, request: Request):
        session(request)
        target = "/" + path
        if not runtime_allowed(request.method, target):
            raise AuthError("This operation is not available through the browser gateway.", 403)
        body = await json_body(request) if request.method == "POST" else None
        if body is not None:
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

    @app.get("/api/control/pi/{path:path}", operation_id="control_read")
    @app.post("/api/control/pi/{path:path}", operation_id="control_write")
    async def control(path: str, request: Request):
        session(request)
        target = "/" + path
        if not owner_allowed(request.method, target):
            raise AuthError("This owner operation is not available through the gateway.", 403)
        if not app.state.config.pi_owner_key:
            raise AuthError("Pi owner-control connection is not configured.", 503)
        body = await json_body(request) if request.method == "POST" else None
        if body is not None:
            admit_write(request, body)
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(forward, request.method,
            app.state.config.pi_url.rstrip("/") + target, "X-Pi-Owner-Key",
            app.state.config.pi_owner_key, body, request.scope["query_string"])

    @app.get("/api/owner/editor-drafts")
    def editor_drafts(request: Request):
        session(request)
        params = request.query_params
        if set(params) - {"limit", "after"} or any(len(params.getlist(key)) != 1 for key in params):
            raise AuthError("Use only editor draft pagination parameters.", 422)
        limit, after = params.get("limit"), params.get("after")
        if ((limit is not None and (not re.fullmatch(r"[0-9]{1,3}", limit) or not 1 <= int(limit) <= 100))
                or (after is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", after))):
            raise AuthError("Invalid editor draft pagination.", 422)
        return forward("GET", app.state.config.toolgate_url.rstrip("/") + "/v2/owner/editor-drafts",
                       "X-ToolGate-Owner-Key", app.state.config.owner_key, None, request.scope["query_string"])

    @app.get("/api/owner/editor-drafts/{identity}")
    @app.post("/api/owner/editor-drafts/{identity}")
    async def editor_draft(identity: str, request: Request):
        session(request)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", identity) or request.scope["query_string"]:
            raise AuthError("Invalid editor draft identity.", 422)
        body = await json_body(request) if request.method == "POST" else None
        if body is not None:
            if (set(body) != {"expected_revision", "document"}
                    or type(body.get("expected_revision")) is not int or body["expected_revision"] < 0
                    or not isinstance(body.get("document"), dict) or body["document"].get("id") != identity):
                raise AuthError("Send an editor document and its expected revision.", 422)
            admit_write(request, body)
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(forward, request.method,
            app.state.config.toolgate_url.rstrip("/") + f"/v2/owner/editor-drafts/{identity}",
            "X-ToolGate-Owner-Key", app.state.config.owner_key, body, b"")

    @app.get("/api/owner/editor-drafts/{identity}/publications")
    @app.get("/api/owner/editor-drafts/{identity}/validation")
    @app.post("/api/owner/editor-drafts/{identity}/publish")
    async def editor_publication(identity: str, request: Request):
        session(request)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", identity) or request.scope["query_string"]:
            raise AuthError("Invalid editor draft identity.", 422)
        operation = request.url.path.rsplit("/", 1)[-1]
        body = await json_body(request) if request.method == "POST" else None
        if body is not None:
            if (set(body) != {"expected_revision", "expected_publication_version", "authorization"}
                    or type(body.get("expected_revision")) is not int or body["expected_revision"] < 1
                    or type(body.get("expected_publication_version")) is not int or body["expected_publication_version"] < 0
                    or body.get("authorization") not in {"auto", "owner_confirmation"}):
                raise AuthError("Select the saved draft, publication version and authorization.", 422)
            admit_write(request, body)
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(forward, request.method,
            app.state.config.toolgate_url.rstrip("/") + f"/v2/owner/editor-drafts/{identity}/{operation}",
            "X-ToolGate-Owner-Key", app.state.config.owner_key, body, b"")

    @app.get("/api/owner/requests")
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

    @app.get("/api/owner/requests/{identity}")
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

    @app.post("/api/owner/requests/{identity}/decision")
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
        return await run_in_threadpool(
            forward,
            "POST",
            app.state.config.toolgate_url.rstrip("/") + f"/v2/owner/requests/{identity}/decision",
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            body,
            b"",
        )

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def dashboard(path: str, request: Request):
        if app.state.dashboard is None:
            return JSONResponse({"detail": "Not found."}, status_code=404)
        return app.state.dashboard.response(path, request)

    return app


async def json_body(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise AuthError("Send application/json.", 415)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 65536:
            raise AuthError("Request is too large; keep it below 64 KiB.", 413)
    return parse_object(raw)


app = create_app()
