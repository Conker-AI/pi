"""Background wall deadlines stop owned I/O and fence late, uncooperative adapters."""

import json
import threading
import time
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_background_thinking import setup

from pi import background_thinking, inference_control, model_roles
from pi.providers import Completion, Message, OllamaProvider, ProviderUnavailable
from pi.store import Store


@pytest.mark.parametrize("continuous", [False, True])
def test_absolute_deadline_disconnects_stalled_and_continuous_http(continuous):
    started, disconnected, finished = threading.Event(), threading.Event(), threading.Event()
    stop_server = threading.Event()
    chunks, failures = [], []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["stream"] is True
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            try:
                while not stop_server.is_set():
                    self.wfile.write(b'{"message":{"content":"piece"},"done":false}\n')
                    self.wfile.flush()
                    chunks.append(1)
                    started.set()
                    if continuous:
                        stop_server.wait(0.025)
                    else:
                        self.connection.settimeout(4)
                        if self.connection.recv(1) == b"":
                            disconnected.set()
                        break
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                disconnected.set()
            finally:
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    provider = OllamaProvider(f"http://127.0.0.1:{server.server_port}")
    token = inference_control.Token()

    def infer():
        try:
            with inference_control.bind(token, background=True), token.deadline(1.5):
                provider.complete_bounded([Message("user", "Analyze")], model="test", timeout=5)
        except Exception as exc:
            failures.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=infer, daemon=True)
    try:
        worker.start()
        assert started.wait(3)
        assert finished.wait(3), "Per-read timeout must not replace the absolute deadline"
        assert disconnected.wait(2)
        worker.join(2)
        assert not worker.is_alive()
        assert failures and token.reason == "deadline"
        assert token._deadline is None and token._callbacks == set()
        if continuous:
            assert len(chunks) >= 3, "The peer must keep producing within its read timeout"
        with inference_control.local_capacity(provider.local_capacity_key, 0.1):
            pass
    finally:
        token.cancel()
        stop_server.set()
        worker.join(3)
        server.shutdown()
        server.server_close()
        serving.join(3)


class LateAdapter:
    name = "one"

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls, self.tokens = [], []
        self.started, self.closed = threading.Event(), threading.Event()

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append(model)
        if model != "think":
            return Completion("Talking reply", model, self.name)
        token = inference_control.current()
        self.tokens.append(token)
        with token.register(self.closed.set):
            self.started.set()
            assert token.stopped.wait(3)
            # Deliberately ignore cancellation, as an old/custom adapter might.
            if self.outcome == "error":
                raise ProviderUnavailable("late provider error")
            return Completion("Late findings must never be published", model, self.name)


def configure_timeout(store, milliseconds=100, *, fallback=False):
    saved = model_roles.load(store)
    assignment = saved["configuration"]["roleSettings"]["roles"]["thinking"]
    assignment["timeoutMs"] = milliseconds
    if fallback:
        assignment.update(eligibleModelIds=["thinker", "a"], failure="fallback", fallbackModelId="a")
    model_roles.save(store, model_roles.Update(
        expected_revision=saved["revision"], configuration=saved["configuration"]
    ))


def join_workers(loop):
    with loop.thinking.lock:
        workers = list(loop.thinking.threads)
    for worker in workers:
        worker.join(3)
        assert not worker.is_alive()


@pytest.mark.parametrize("outcome", ["result", "error"])
def test_thinking_expiry_fences_late_result_and_never_falls_back(tmp_path, outcome):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, _ = setup(store)
        configure_timeout(store, fallback=True)
        provider = LateAdapter(outcome)
        loop.router.local = provider
        session = store.create_session()
        result = loop.run_turn(session, "Requested analysis", think=True)
        assert result["message"]["content"] == "Talking reply"
        assert provider.started.wait(2)
        join_workers(loop)
        row = background_thinking.list_for_session(store, session)[0]
        assert row["status"] == "failed" and row["result"] is None
        assert "time limit" in row["detail"] and "foreground" not in row["detail"]
        assert provider.closed.is_set()
        assert provider.calls.count("think") == 1 and provider.calls.count("actual-a") == 1
        assert provider.tokens[0].reason == "deadline"
        assert provider.tokens[0]._deadline is None and provider.tokens[0]._callbacks == set()
        assert loop.thinking.slots.acquire(blocking=False)
        assert loop.thinking.slots.acquire(blocking=False)
        loop.thinking.slots.release()
        loop.thinking.slots.release()


def test_queue_wait_does_not_consume_frozen_thinking_deadline(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, provider = setup(store)
        configure_timeout(store, milliseconds=100)
        deadlines = []
        original = inference_control.Token.deadline

        @contextmanager
        def observed(token, seconds):
            deadlines.append(seconds)
            with original(token, seconds):
                yield

        monkeypatch.setattr(inference_control.Token, "deadline", observed)
        loop.thinking.slots.acquire()
        loop.thinking.slots.acquire()
        session = store.create_session()
        loop.run_turn(session, "Queued explicit analysis", think=True)
        # Change live settings while the task is queued: its snapshot stays at100ms.
        configure_timeout(store, milliseconds=2000)
        time.sleep(0.15)
        assert deadlines == [] and not provider.started.is_set()
        assert background_thinking.list_for_session(store, session)[0]["status"] == "queued"
        provider.release.set()
        loop.thinking.slots.release()
        loop.thinking.slots.release()
        join_workers(loop)
        assert deadlines == [0.1]
        assert background_thinking.list_for_session(store, session)[0]["status"] == "completed"


@pytest.mark.parametrize("outcome", ["success", "error", "owner", "timer_start_error"])
def test_deadline_timer_is_disposed_and_stale_fire_cannot_cancel(outcome, monkeypatch):
    timers = []

    class Timer:
        def __init__(self, seconds, callback, args, kwargs):
            self.callback, self.args, self.kwargs = callback, args, kwargs
            self.canceled, self.daemon = False, False
            timers.append(self)

        def start(self):
            if outcome == "timer_start_error":
                raise RuntimeError("synthetic thread limit")

        def cancel(self):
            self.canceled = True

        def fire(self):
            self.callback(*self.args, **self.kwargs)

    monkeypatch.setattr(inference_control.threading, "Timer", Timer)
    token = inference_control.Token()
    try:
        with token.deadline(1):
            if outcome == "error":
                raise ValueError("synthetic failure")
            if outcome == "owner":
                token.cancel("owner")
    except (ValueError, RuntimeError):
        pass
    assert timers[0].daemon and timers[0].canceled
    assert token._deadline is None
    timers[0].fire()
    assert token.reason == ("owner" if outcome == "owner" else None)


def test_monotonic_check_fences_late_result_even_if_timer_was_delayed(monkeypatch):
    instant = [100.0]
    monkeypatch.setattr(inference_control.time, "monotonic", lambda: instant[0])
    token = inference_control.Token()
    closed = threading.Event()
    with (
        pytest.raises(inference_control.InferenceCancelled),
        token.register(closed.set),
        token.deadline(1),
    ):
        instant[0] = 102.0
    assert token.reason == "deadline" and closed.is_set()
    assert token._deadline is None and token._callbacks == set()
