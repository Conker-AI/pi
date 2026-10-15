"""Cancellation ends actual owned I/O; foreground admission cannot overlap it."""

import json
import threading
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from test_background_thinking import setup

from pi import background_thinking, inference_control
from pi.chatgpt_provider import ChatGPTProvider
from pi.claude_code_provider import ClaudeCodeProvider
from pi.providers import Completion, Message, OllamaProvider
from pi.store import Store


def join(thread):
    thread.join(3)
    assert not thread.is_alive(), "owned inference did not stop"


def test_ollama_cancel_interrupts_blocked_socket_and_releases_local_capacity():
    started, disconnected, finished = threading.Event(), threading.Event(), threading.Event()
    payloads, failures = [], []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_POST(self):
            payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write(b'{"message":{"content":""},"done":false}\n')
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

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    token = inference_control.Token()
    provider = OllamaProvider(f"http://127.0.0.1:{server.server_port}")

    def infer():
        try:
            with inference_control.bind(token, background=True):
                provider.complete_bounded([Message("user", "Analyze")], model="test", timeout=20)
        except Exception as exc:
            failures.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=infer, daemon=True)
    try:
        worker.start()
        assert started.wait(2)
        token.cancel()
        assert finished.wait(3)
        assert disconnected.wait(2)
        join(worker)
        assert failures
        assert payloads[0]["stream"] is True
        with inference_control.local_capacity(provider.local_capacity_key, 0.1):
            pass
    finally:
        token.cancel()
        server.shutdown()
        server.server_close()
        join(serving)


@pytest.mark.parametrize("adapter", [ChatGPTProvider, ClaudeCodeProvider])
def test_native_cancel_targets_only_the_bound_request(adapter):
    started, canceled = threading.Event(), threading.Event()
    seen, errors = [], []
    token = inference_control.Token()

    def handle(request):
        payload = json.loads(request.content)
        seen.append((request.url.path, payload))
        if request.url.path == "/cancel":
            canceled.set()
            return httpx.Response(
                200, json={"request_id": payload["request_id"], "status": "cancel_requested"}
            )
        started.set()
        assert canceled.wait(3)
        return httpx.Response(503, json={"error": "canceled"})

    provider = adapter("", transport=httpx.MockTransport(handle), request_cancellation=True)

    def infer():
        try:
            with inference_control.bind(token, background=True):
                provider.complete_bounded([Message("user", "Analyze")], model="test", timeout=20)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=infer, daemon=True)
    worker.start()
    assert started.wait(2)
    token.cancel()
    join(worker)
    assert errors and len(seen) == 2
    assert seen[0][1]["request_id"] == seen[1][1]["request_id"]
    assert set(seen[1][1]) == {"request_id"}


def test_legacy_subscription_worker_keeps_its_original_payload_when_thinking():
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            text="\n".join(
                [
                    json.dumps({"type": "text", "delta": "Findings"}),
                    json.dumps(
                        {
                            "type": "done",
                            "model": "test",
                            "inputTokens": 1,
                            "outputTokens": 1,
                            "cachedTokens": None,
                        }
                    ),
                ]
            ),
        )

    provider = ChatGPTProvider("/legacy.sock", transport=httpx.MockTransport(handle))
    token = inference_control.Token()
    with inference_control.bind(token, background=True):
        result = provider.complete_bounded([Message("user", "Analyze")], model="test", timeout=20)
    token.cancel()
    assert result.text == "Findings" and len(seen) == 1
    assert set(seen[0]) == {"model", "messages", "timeout"}


class SharedLocal:
    name = "one"
    local_capacity_key = "test:shared-local"

    def __init__(self, block_again=False):
        self.started = [threading.Event(), threading.Event()]
        self.calls = 0
        self.block_again = block_again
        self.messages = []
        self.tokens = []

    def complete_bounded(self, messages, *, model, timeout):
        with inference_control.local_capacity(self.local_capacity_key, 3):
            if model == "think":
                attempt = self.calls
                self.calls += 1
                self.messages.append(messages)
                self.started[attempt].set()
                if attempt == 0 or self.block_again:
                    token = inference_control.current()
                    self.tokens.append(token)
                    # Foreground admission may perform slow local DB work.
                    # Stay active until real preemption or test cleanup stops us.
                    token.stopped.wait()
                    token.check()
                return Completion("Frozen findings", model, self.name)
            return Completion("Talking stays responsive", model, self.name)


@pytest.mark.parametrize("second_preemption", [False, True])
def test_foreground_preempts_local_thinker_with_at_most_one_frozen_requeue(
    tmp_path, second_preemption, monkeypatch
):
    @contextmanager
    def no_wall_deadline(token, _seconds):
        # This test owns admission/preemption ordering. Timer expiry is covered
        # by test_thinking_deadline, independently of foreground DB latency.
        token.check()
        yield
        token.check()

    monkeypatch.setattr(inference_control.Token, "deadline", no_wall_deadline)
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, _ = setup(store)
        provider = SharedLocal(block_again=second_preemption)
        loop.router.local = provider
        try:
            original = store.create_session()
            loop.run_turn(original, "Keep this exact input", think=True)
            assert provider.started[0].wait(5)
            reply = loop.run_turn(original, "An urgent ordinary follow-up")
            assert reply["message"]["content"] == "Talking stays responsive"
            assert provider.started[1].wait(5)
            if second_preemption:
                loop.run_turn(original, "Another ordinary follow-up")
            with loop.thinking.lock:
                workers = list(loop.thinking.threads)
            for worker in workers:
                join(worker)
            row = background_thinking.list_for_session(store, original)[0]
            assert row["status"] == ("skipped" if second_preemption else "completed")
            assert provider.calls == 2
            assert provider.messages[0] == provider.messages[1]
            assert not any(
                "ordinary follow-up" in message.content for message in provider.messages[1]
            )
            assert row["result"] == (None if second_preemption else "Frozen findings")
            assert len(provider.tokens) == (2 if second_preemption else 1)
            assert all(token.reason == "preempted" for token in provider.tokens)
        finally:
            loop.thinking.close()
            with loop.thinking.lock:
                workers = list(loop.thinking.threads)
            for worker in workers:
                join(worker)
