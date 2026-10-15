"""Loopback speed must preserve TLS, proxy, redirect and action boundaries."""

import hashlib
import json
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from gateway.__main__ import create_certificate
from pi import inference_control, live_stream
from pi.decision_provider import DecisionProvider
from pi.loopback_http import verification
from pi.providers import Message, OllamaProvider, ProviderUnavailable
from pi.toolgate import ToolGateClient, ToolGateUnavailable


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/base", "http://[::1]:8000/base"])
def test_only_numeric_loopback_http_uses_fresh_empty_verified_context(url):
    context = verification(url)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.cert_store_stats()["x509_ca"] == 0
    assert verification(url) is not context


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8000",
        "https://[::1]:8000",
        "https://api.example.test",
        "http://localhost:8000",
        "http://127.0.0.1.example.test",
        "http://example.test",
        "http://127.0.0.2",
        "http://127.1",
        "http://2130706433",
        "http://10.0.0.1",
        "http://user@127.0.0.1",
        "http://user:password@[::1]",
        "http://127.0.0.1:invalid",
    ],
)
def test_other_destinations_keep_default_verification(url):
    assert verification(url) is True


def test_https_still_loads_default_cas_without_environment_override(monkeypatch):
    import certifi

    calls = []

    def observed_default(**kwargs):
        calls.append(kwargs)
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setenv("SSL_CERT_FILE", "not-an-approved-ca-file")
    monkeypatch.setattr(ssl, "create_default_context", observed_default)
    with httpx.Client(
        verify=verification("https://127.0.0.1"), trust_env=False, follow_redirects=False
    ):
        pass
    assert calls == [{"cafile": certifi.where()}]


@contextmanager
def peer(tls=None):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        def respond(self, body=None):
            seen.append((self.path, dict(self.headers), body))
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "/sink")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/api/tags":
                result = {"models": [{"name": "synthetic"}]}
            elif self.path == "/api/chat":
                result = {
                    "message": {"content": "Synthetic reply"},
                    "model": "synthetic",
                    "done": True,
                }
            elif self.path in {"/v2/agent/tools", "/v2/agent/published-workflows"}:
                result = []
            elif self.path == "/v2/agent/status":
                result = {"lockdown": False}
            elif self.path == "/v2/agent/policy-summary":
                result = {"lockdown": False, "scopePatterns": [], "tools": []}
                canonical = json.dumps(result, sort_keys=True, separators=(",", ":"))
                result["digest"] = hashlib.sha256(canonical.encode()).hexdigest()
            elif self.path == "/health":
                result = {"status": "ready", "model": "synthetic"}
            elif self.path == "/v1/choose":
                result = {
                    "choice": "a",
                    "confidence": 1,
                    "probabilities": {"a": 1},
                    "model": "synthetic",
                }
            else:
                result = {"action_id": "saved", "ok": True, "result": {"synthetic": True}}
            payload = json.dumps(result).encode() + b"\n"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http{'s' if tls else ''}://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_loopback_hot_paths_make_real_requests_without_ca_loading_or_ambient_proxies(monkeypatch):
    def unwanted_ca_load(*_args, **_kwargs):
        raise AssertionError("Loopback HTTP must not load a CA bundle")

    monkeypatch.setattr(ssl, "create_default_context", unwanted_ca_load)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with peer() as (url, seen):
        local = OllamaProvider(url, model="synthetic")
        assert local.health()["status"] == "ok"
        assert (
            local.complete([Message("user", "Test")], model="synthetic").text == "Synthetic reply"
        )
        with inference_control.bind(inference_control.Token()):
            assert local.complete([], model="synthetic").text == "Synthetic reply"
        with live_stream.open_reply("synthetic-http"), live_stream.answering():
            assert local.complete([], model="synthetic").text == "Synthetic reply"
        gate = ToolGateClient(url, "synthetic-execution-key")
        assert gate.tools() == [] and gate.health()["status"] == "ok"
        assert gate.policy_summary()["tools"] == []
        assert gate.invoke("synthetic", {}, action_id="saved").ok
        assert gate.check_action("saved", "synthetic").ok
        decision = DecisionProvider(url, "synthetic-decision-key" * 2)
        assert decision.health()["status"] == "ok"
        assert decision.choose("Test", "Choose", {"a": "Only choice"})["choice"] == "a"
        assert len(seen) == 12
        for path, headers, _body in seen:
            if path.startswith("/v2/"):
                assert headers["X-ToolGate-Execution-Key"] == "synthetic-execution-key"
            elif path in {"/health", "/v1/choose"}:
                assert headers["X-Decision-Key"] == "synthetic-decision-key" * 2


def test_loopback_redirects_are_still_refused():
    with peer() as (url, seen):
        target = url + "/redirect"
        assert OllamaProvider(target).health()["status"] == "unavailable"
        with pytest.raises(ToolGateUnavailable):
            ToolGateClient(target, "synthetic-key").tools()
        with pytest.raises(ProviderUnavailable):
            DecisionProvider(target, "synthetic-key" * 3).choose("Test", "Choose", {"a": "Only"})
        assert len(seen) == 3 and all(path.startswith("/redirect/") for path, *_ in seen)


def test_empty_loopback_context_refuses_accidental_https_before_http_request(tmp_path):
    cert, key = create_certificate(tmp_path, "127.0.0.1")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    with peer(tls) as (url, seen):
        with (
            httpx.Client(
                verify=verification(url.replace("https:", "http:", 1)),
                trust_env=False,
                follow_redirects=False,
                timeout=5,
            ) as client,
            pytest.raises(httpx.ConnectError),
        ):
            client.get(url + "/api/tags")
        assert seen == []
        # The test peer itself is valid when its exact CA is explicitly trusted.
        with httpx.Client(
            verify=ssl.create_default_context(cafile=cert),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            assert client.get(url + "/api/tags").status_code == 200
        assert len(seen) == 1
