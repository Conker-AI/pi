"""Outbound HTTP must use configured destinations, never ambient proxy routing."""

from __future__ import annotations

import ast
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from pi.memory import MemoryClient
from pi.providers import OllamaProvider
from pi.toolgate import ToolGateClient


class _JsonHandler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):
        type(self).hits.append(self.path)
        bodies = {
            "/api/tags": {"models": [{"name": "local"}]},
            "/health": {"status": "ok"},
            "/v2/agent/status": {"lockdown": False},
        }
        body = json.dumps(bodies.get(self.path, {"ok": True})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class _ProxyHandler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):
        type(self).hits.append(self.path)
        self.send_response(502)
        self.end_headers()

    def log_message(self, *_args):
        pass


def _server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_internal_clients_ignore_ambient_proxy_variables(monkeypatch):
    origin = _server(_JsonHandler)
    proxy = _server(_ProxyHandler)
    _JsonHandler.hits = []
    _ProxyHandler.hits = []
    origin_url = f"http://127.0.0.1:{origin.server_port}"
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, proxy_url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    memory = MemoryClient(origin_url, "ingest", "read")
    try:
        control = httpx.get(origin_url + "/control", trust_env=True, follow_redirects=False)
        assert control.status_code == 502 and len(_ProxyHandler.hits) == 1

        assert ToolGateClient(origin_url, "execution").health() == {"status": "ok"}
        assert OllamaProvider(origin_url, model="local").health() == {"status": "ok"}
        assert memory.health() == {"status": "ok"}

        assert _ProxyHandler.hits == [_ProxyHandler.hits[0]]
        assert _JsonHandler.hits == ["/v2/agent/status", "/api/tags", "/health"]
    finally:
        memory.close()
        origin.shutdown()
        origin.server_close()
        proxy.shutdown()
        proxy.server_close()


def test_every_runtime_httpx_entry_point_disables_environment_and_redirects():
    root = Path(__file__).parents[1]
    failures = []
    entry_points = {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "request",
        "stream",
        "Client",
        "AsyncClient",
    }
    for package in (root / "pi", root / "gateway"):
        for path in package.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "httpx"
                    and node.func.attr in entry_points
                ):
                    continue
                options = {keyword.arg: keyword.value for keyword in node.keywords}
                for option in ("trust_env", "follow_redirects"):
                    value = options.get(option)
                    if not isinstance(value, ast.Constant) or value.value is not False:
                        failures.append(
                            f"{path.relative_to(root)}:{node.lineno} missing {option}=False"
                        )
    assert failures == []
