"""Same-origin browser authentication and an explicit service-operation allowlist."""

from __future__ import annotations

import hmac
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from pi.owner.terminal import TerminalError

from . import auth_routes, host_routes, owner_routes, service_routes, terminal_routes
from .dashboard import API_CSP, UI_CSP, DashboardAssets
from .diagnostics import collect_diagnostics
from .operations import fingerprint
from .store import AuthError, AuthStore
from .terminal_client import RemoteTerminal, TerminalSidecar
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
    terminal_socket: str = ""
    terminal_workspace_label: str = ""
    pi_owner_key: str = ""
    toolgate_execution_key: str = ""
    cookie_namespace: str = ""
    provider_control_socket: str = ""
    provider_control_url: str = ""
    provider_control_key: str = ""

    @property
    def cookie_name(self) -> str:
        return f"{COOKIE}-{self.cookie_namespace}" if self.cookie_namespace else COOKIE

    def validate(self) -> None:
        if self.provider_control_url:
            from pi.models.private_transport import PrivateTransport

            PrivateTransport(self.provider_control_url, self.provider_control_key).close()
            if self.provider_control_socket:
                raise ValueError("Choose one private provider transport.")
        if self.provider_control_socket and (
            not os.path.isabs(self.provider_control_socket)
            or any(c in self.provider_control_socket for c in "\x00\r\n")
        ):
            raise ValueError("Set GATEWAY_PROVIDER_CONTROL_SOCKET to an absolute private socket.")
        if self.cookie_namespace and not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,39}", self.cookie_namespace
        ):
            raise ValueError(
                "GATEWAY_COOKIE_NAMESPACE must be 1-40 lowercase letters, digits or hyphens."
            )
        if self.toolgate_execution_key and (
            not self.toolgate_execution_key.startswith("tgx_")
            or len(self.toolgate_execution_key) < 32
            or self.toolgate_execution_key in {self.pi_key, self.owner_key, self.pi_owner_key}
        ):
            raise ValueError(
                "Provision a distinct scoped ToolGate execution credential on the host."
            )
        if self.pi_owner_key and (
            len(self.pi_owner_key) < 32 or self.pi_owner_key in {self.pi_key, self.owner_key}
        ):
            raise ValueError("Provision a distinct Pi owner-control key of at least 32 characters.")
        if bool(self.terminal_socket) != bool(self.terminal_workspace_label):
            raise ValueError("Configure terminal socket and workspace label together.")
        if self.terminal_socket and (
            not os.path.isabs(self.terminal_socket)
            or not 1 <= len(self.terminal_workspace_label) <= 120
            or any(character in "\x00\r\n" for character in self.terminal_workspace_label)
        ):
            raise ValueError("Terminal configuration is invalid.")
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
            os.environ.get("GATEWAY_TERMINAL_SOCKET", ""),
            os.environ.get("GATEWAY_TERMINAL_WORKSPACE_LABEL", ""),
            os.environ.get("GATEWAY_PI_OWNER_KEY", ""),
            os.environ.get("GATEWAY_TOOLGATE_EXECUTION_KEY", ""),
            os.environ.get("GATEWAY_COOKIE_NAMESPACE", ""),
            os.environ.get("GATEWAY_PROVIDER_CONTROL_SOCKET", ""),
            os.environ.get("GATEWAY_PROVIDER_CONTROL_URL", ""),
            os.environ.get("GATEWAY_PROVIDER_CONTROL_KEY", ""),
        )


def create_app(
    config: Config | None = None,
    *,
    store: AuthStore | None = None,
    transport: httpx.BaseTransport | None = None,
    stream_transport: httpx.AsyncBaseTransport | None = None,
    terminal_factory=None,
    provider_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = config or Config.environment()
        settings.validate()
        app.state.config = settings
        app.state.provider_transport = provider_transport
        if settings.provider_control_url:
            from pi.models.private_transport import PrivateTransport

            app.state.provider_transport = PrivateTransport(
                settings.provider_control_url, settings.provider_control_key
            )
        app.state.dashboard = (
            DashboardAssets(settings.dashboard_dir) if settings.dashboard_dir else None
        )
        app.state.auth = store or AuthStore(
            settings.database, idle_seconds=settings.idle_timeout_seconds
        )
        app.state.terminal_readiness = None
        app.state.terminal_sidecar = None
        if settings.terminal_socket:
            if terminal_factory is None:
                sidecar = TerminalSidecar(settings.terminal_socket)
                app.state.terminal_readiness = sidecar.readiness()
                app.state.terminal_sidecar = sidecar

                def factory(socket_path, workspace_label, request_identity):
                    del socket_path
                    return RemoteTerminal(sidecar, workspace_label, request_identity)

            else:
                factory = terminal_factory
            app.state.terminals = Terminals(
                settings.terminal_socket,
                settings.terminal_workspace_label,
                factory=factory,
            )
        else:
            app.state.terminals = None
        with httpx.Client(
            transport=transport, timeout=660, follow_redirects=False, trust_env=False
        ) as client:
            app.state.client = client
            try:
                yield
            finally:
                if settings.provider_control_url:
                    app.state.provider_transport.shutdown()
                if app.state.terminals:
                    app.state.terminals.close()
                if app.state.terminal_sidecar:
                    app.state.terminal_sidecar.close()

    app = FastAPI(
        title="Conker browser gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "Request validation failed."}, status_code=422)

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
        token = request.cookies.get(app.state.config.cookie_name, "")
        value = auth.session(token, authenticated=authenticated)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:  # noqa: SIM102 - group unsafe-method checks
            if request.headers.get("origin") != app.state.config.origin or not hmac.compare_digest(
                request.headers.get("x-csrf-token", "").encode(), value["csrf"].encode()
            ):
                raise AuthError("Request verification failed. Reload Conker and try again.", 403)
        return value

    def cookie(response: JSONResponse, token: str) -> JSONResponse:
        response.set_cookie(
            app.state.config.cookie_name,
            token,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    def upstream(
        method: str,
        url: str,
        key_header: str,
        key: str,
        body: dict | None,
        query: bytes,
        *,
        timeout: float = 660,
        extra_headers: dict | None = None,
    ) -> httpx.Response:
        outgoing = app.state.client.build_request(
            method,
            url,
            headers={key_header: key, **(extra_headers or {})},
            json=body,
            params=query.decode("ascii") if query else None,
            timeout=timeout,
        )
        # A shared HTTP client must not turn a service's Set-Cookie into later authority.
        outgoing.headers.pop("cookie", None)
        return app.state.client.send(outgoing)

    def forward(
        method: str,
        url: str,
        key_header: str,
        key: str,
        body: dict | None,
        query: bytes,
        extra_headers=None,
    ):
        if not key:
            raise AuthError(
                "Owner approval channel is not configured. "
                "Provision its scoped credential on the host.",
                503,
            )
        try:
            response = upstream(
                method, url, key_header, key, body, query, extra_headers=extra_headers
            )
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
            request.cookies.get(app.state.config.cookie_name, ""),
            request.headers.get("x-conker-verification", ""),
            binding,
        )

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
        checks["owner_terminal"] = {
            "status": "ok" if app.state.terminals else "not_configured",
            "reason": (
                "Isolated terminal sidecar ready"
                if app.state.terminals
                else "Terminal sidecar is not configured"
            ),
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

    @app.get("/api/diagnostics")
    def diagnostics(request: Request):
        session(request)
        if request.scope["query_string"]:
            raise AuthError("Diagnostics do not accept query parameters.", 422)
        return collect_diagnostics(app.state.config, app.state.auth, app.state.client)

    # Registered in their original order: the Pi proxies and the dashboard end in catch-all paths.
    app.include_router(auth_routes.router(app, session, cookie, admit_write))
    app.include_router(host_routes.router(app, session, admit_write))
    app.include_router(terminal_routes.router(app, session, admit_write))
    app.include_router(service_routes.router(app, session, forward, admit_write, stream_transport))
    app.include_router(owner_routes.router(app, session, forward, admit_write))

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def dashboard(path: str, request: Request):
        if app.state.dashboard is None:
            return JSONResponse({"detail": "Not found."}, status_code=404)
        return app.state.dashboard.response(path, request)

    return app


app = create_app()
