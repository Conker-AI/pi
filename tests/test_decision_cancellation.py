"""Synthetic classifier sockets only; Stop must not wait for a routing timeout."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from test_decision_provider import setup
from test_model_roles import Adapter

from pi.models import roles as model_roles
from pi.models.decision import DecisionProvider
from pi.models.providers import Message
from pi.turns import inference_control


def decision():
    return {
        "choice": "a",
        "confidence": 0.8,
        "model": "synthetic-laya",
        "probabilities": {"a": 0.8, "b": 0.2},
    }


@contextmanager
def stalled_server(headers):
    started, disconnected = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_POST(self):
            requests.append(
                (self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            )
            if headers:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b'{"choice":')
                self.wfile.flush()
            # Normal classifier inference waits before any response headers.
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
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", started, disconnected, requests
    finally:
        server.shutdown()
        server.server_close()
        serving.join(2)


@pytest.mark.parametrize("headers", [False, True], ids=["before-headers", "partial-body"])
def test_stop_closes_owned_classifier_connection_without_answer_or_fallback(headers):
    token, finished = inference_control.Token(), threading.Event()
    failures = []
    configuration, _ = setup(httpx.MockTransport(lambda _: pytest.fail("unused transport")))
    configuration["roleSettings"]["roles"]["routing"]["timeoutMs"] = 15000
    configuration["roleSettings"]["roles"]["answer"].update(failure="fallback", fallbackModelId="b")
    first, second = Adapter('{"modelId":"b"}'), Adapter()
    with stalled_server(headers) as (url, started, disconnected, requests):
        provider = DecisionProvider(url, "x" * 32)

        def work():
            try:
                with inference_control.bind(token):
                    model_roles.dispatch(
                        configuration,
                        "answer",
                        [Message("user", "Synthetic request")],
                        {"decisions": provider, "one": first, "two": second},
                        guard=inference_control.check,
                    )
            except Exception as exc:
                failures.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=work, daemon=True)
        try:
            worker.start()
            assert started.wait(3)
            token.cancel("owner")
            assert finished.wait(2), "Stop must not await the configured15-second routing timeout"
            assert disconnected.wait(2)
            assert len(failures) == 1
            assert isinstance(failures[0], inference_control.InferenceCancelled)
            assert first.calls == second.calls == []
            assert len(requests) == 1 and requests[0][0] == "/v1/choose"
            assert token._callbacks == set()
        finally:
            token.cancel()
            worker.join(3)
        assert not worker.is_alive()


def test_pre_cancelled_parent_token_prevents_classifier_request():
    token = inference_control.Token()
    provider = DecisionProvider(
        "http://127.0.0.1:8060",
        "x" * 32,
        transport=httpx.MockTransport(lambda _: pytest.fail("must not send")),
    )
    with inference_control.bind(token):
        token.cancel()
        with pytest.raises(inference_control.InferenceCancelled):
            provider.choose("request", "choose", {"a": "A", "b": "B"})
    assert token._callbacks == set()


def test_late_success_is_fenced_and_callbacks_do_not_leak_to_later_requests():
    token = inference_control.Token()
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["X-Decision-Key"] == "x" * 32
        if len(calls) == 1:
            assert inference_control.current() is token
            token.cancel()
        return httpx.Response(200, json=decision())

    provider = DecisionProvider(
        "http://127.0.0.1:8060", "x" * 32, transport=httpx.MockTransport(handler)
    )
    with inference_control.bind(token), pytest.raises(inference_control.InferenceCancelled):
        provider.choose("request", "choose", {"a": "A", "b": "B"})
    assert token._callbacks == set()
    later = inference_control.Token()
    with inference_control.bind(later):
        assert provider.choose("later", "choose", {"a": "A", "b": "B"}) == decision()
    assert later._callbacks == set()
    assert len(calls) == 2


@pytest.mark.parametrize(
    "event", ["connection.connect_tcp.complete", "connection.start_tls.complete"]
)
def test_connected_handle_arriving_after_stop_is_closed_before_progress(event):
    token = inference_control.Token()

    class Stream:
        closed = False
        shut_down = False

        def get_extra_info(self, name):
            assert name == "socket"
            return self

        def shutdown(self, _):
            self.shut_down = True

        def close(self):
            self.closed = True

    stream = Stream()

    def handler(request):
        token.cancel()
        request.extensions["trace"](event, {"return_value": stream})
        pytest.fail("Late connected handle must not continue after Stop")

    provider = DecisionProvider(
        "http://127.0.0.1:8060", "x" * 32, transport=httpx.MockTransport(handler)
    )
    with inference_control.bind(token), pytest.raises(inference_control.InferenceCancelled):
        provider.choose("request", "choose", {"a": "A", "b": "B"})
    assert stream.closed and stream.shut_down
    assert token._callbacks == set()
