"""Synthetic providers only: scoped transport cancellation without account access."""

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from pi import direct_providers, inference_control, openrouter
from pi.direct_providers import AnthropicProvider, OpenAIProvider
from pi.openrouter import ModelInfo, OpenRouterProvider, PaidModelRefused
from pi.providers import Message, ProviderUnavailable

KINDS = ("openai", "anthropic", "openrouter")


def adapter(kind, *, paid=True):
    if kind == "openrouter":
        provider = OpenRouterProvider("synthetic-key", allow_paid=paid)
        provider._catalogue = {
            "requested": ModelInfo(
                "requested",
                4096,
                0.01,
                0.01,
                False,
                pricing={"prompt": "0.01", "completion": "0.01", "request": "0"},
            )
        }
        provider._fetched_at = time.monotonic()
        return provider
    cls = OpenAIProvider if kind == "openai" else AnthropicProvider
    return cls("synthetic-key", allow_paid=paid)


def events(kind):
    if kind == "anthropic":
        values = [
            {"type": "message_start", "message": {"model": "actual", "usage": {"input_tokens": 2}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Findings"},
            },
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
    else:
        values = [
            {
                "model": "actual",
                "choices": [
                    {"index": 0, "delta": {"content": "Findings"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "cost": 0.03},
            }
        ]
    body = "".join("data: " + json.dumps(value) + "\n\n" for value in values)
    return (body + ("data: [DONE]\n\n" if kind != "anthropic" else "")).encode()


@contextmanager
def stalled_server():
    started, disconnected = threading.Event(), threading.Event()
    seen = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def stalled(self):
            content = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append((self.command, json.loads(content) if content else None))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b": waiting\n\n")
            self.wfile.flush()
            started.set()
            self.connection.settimeout(5)
            try:
                if self.connection.recv(1) == b"":
                    disconnected.set()
            except (ConnectionResetError, ConnectionAbortedError):
                disconnected.set()
            except OSError:
                pass
            self.close_connection = True

        do_POST = stalled
        do_GET = stalled

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", started, disconnected, seen
    finally:
        server.shutdown()
        server.server_close()
        serving.join(3)
        assert not serving.is_alive()


@pytest.mark.parametrize("kind", KINDS)
def test_cancellation_interrupts_owned_hosted_socket_without_retry(kind, monkeypatch):
    provider, token = adapter(kind), inference_control.Token()
    failures, finished = [], threading.Event()
    with stalled_server() as (url, started, disconnected, seen):
        if kind == "openrouter":
            monkeypatch.setattr(openrouter, "CHAT_URL", url)
        else:
            provider.url = url

        def infer():
            try:
                with inference_control.bind(token, background=True):
                    provider.complete_bounded(
                        [Message("user", "Analyze")], model="requested", timeout=15
                    )
            except Exception as exc:
                failures.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=infer, daemon=True)
        try:
            worker.start()
            assert started.wait(3)
            token.cancel()
            assert finished.wait(3)
            assert disconnected.wait(2)
            worker.join(2)
            assert not worker.is_alive()
            assert len(failures) == 1 and isinstance(
                failures[0], inference_control.InferenceCancelled
            )
            assert len(seen) == 1 and seen[0][1]["stream"] is True
        finally:
            token.cancel()
            worker.join(3)


@pytest.mark.parametrize("kind", KINDS)
def test_successful_bound_request_preserves_wire_authority_usage_and_later_calls(kind, monkeypatch):
    provider = adapter(kind)
    original_client = httpx.Client
    requests, clients = [], []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200, content=events(kind), headers={"Content-Type": "text/event-stream"}
        )

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        result = original_client(transport=httpx.MockTransport(handle), **kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr(direct_providers.httpx, "Client", client)
    for _ in range(2):
        with inference_control.bind(inference_control.Token(), background=True):
            result = provider.complete_bounded(
                [Message("system", "Rules"), Message("user", "Analyze")],
                model="requested",
                timeout=1.5,
            )
        assert (result.text, result.model, result.input_tokens, result.output_tokens) == (
            "Findings",
            "actual",
            2,
            1,
        )
    assert len(requests) == 2 and all(client.is_closed for client in clients)
    body = json.loads(requests[0].content)
    assert body["stream"] is True and body["model"] == "requested"
    if kind == "anthropic":
        assert requests[0].headers["x-api-key"] == "synthetic-key"
        assert body["system"] == [{"type": "text", "text": "Rules"}] and body["max_tokens"] == 4096
    else:
        assert requests[0].headers["authorization"] == "Bearer synthetic-key"
        assert body["messages"][0] == {"role": "system", "content": "Rules"}
        if kind == "openai":
            assert body["store"] is False and body["stream_options"] == {"include_usage": True}
        else:
            assert result.cost_usd == 0.03


@pytest.mark.parametrize("kind", KINDS)
def test_bound_request_does_not_bypass_payment_or_pre_cancel_guard(kind, monkeypatch):
    def no_client(*_args, **_kwargs):
        pytest.fail("Stopped or unauthorized inference must not make a request")

    monkeypatch.setattr(direct_providers.httpx, "Client", no_client)
    with (
        inference_control.bind(inference_control.Token(), background=True),
        pytest.raises((PaidModelRefused, ProviderUnavailable)),
    ):
        adapter(kind, paid=False).complete_bounded(
            [Message("user", "Analyze")], model="requested", timeout=1
        )
    token = inference_control.Token()
    with inference_control.bind(token, background=True):
        token.cancel()
        with pytest.raises(inference_control.InferenceCancelled):
            adapter(kind).complete_bounded(
                [Message("user", "Analyze")], model="requested", timeout=1
            )


def test_openrouter_catalogue_body_is_also_cancelable(monkeypatch):
    provider, token = OpenRouterProvider("synthetic-key"), inference_control.Token()
    failures = []
    with stalled_server() as (url, started, disconnected, seen):
        monkeypatch.setattr(openrouter, "CATALOGUE_URL", url)

        def fetch():
            try:
                with inference_control.bind(token, background=True):
                    provider.catalogue()
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=fetch, daemon=True)
        try:
            worker.start()
            assert started.wait(3)
            token.cancel()
            worker.join(3)
            assert not worker.is_alive() and disconnected.wait(2)
            assert len(failures) == 1 and isinstance(
                failures[0], inference_control.InferenceCancelled
            )
            assert seen == [("GET", None)] and provider._catalogue == {}
        finally:
            token.cancel()
            worker.join(3)
