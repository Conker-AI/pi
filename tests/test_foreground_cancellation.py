"""Owner Stop interrupts silent inference without abandoning saved effects."""

import json
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_loop import Recorder, loop_with

from pi.conversation import settings as session_settings
from pi.conversation import submissions
from pi.conversation.store import Store
from pi.models.providers import OllamaProvider, ProviderUnavailable
from pi.turns import control as turn_control
from pi.turns import foreground_inference, inference_control
from pi.turns.failures import TurnFailed


@pytest.mark.parametrize("stop_by", ["turn", "submission"])
def test_stop_interrupts_silent_ollama_and_retains_exact_input(tmp_path, stop_by):
    started, disconnected, finished = (threading.Event() for _ in range(3))
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
            # Real socket read stays blocked: no visible text and no more chunks.
            self.wfile.write(b'{"message":{"thinking":"private"},"done":false}\n')
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
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        provider = OllamaProvider(f"http://127.0.0.1:{server.server_port}")
        loop = loop_with(store, provider)
        request_id = "silent_foreground_stop"

        def infer():
            try:
                loop.run_turn(session, "Synthetic greeting", request_id=request_id)
            except Exception as exc:
                failures.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=infer, daemon=True)
        try:
            worker.start()
            assert started.wait(30)
            receipt = submissions.get(store, request_id)
            if stop_by == "turn":
                turn_control.cancel(store, receipt["turn_id"])
            else:
                turn_control.cancel_submission(store, request_id)
            assert finished.wait(30), "Stop must not wait for the next visible token"
            assert disconnected.wait(30)
            worker.join(30)
            assert len(failures) == 1 and isinstance(failures[0], TurnFailed)
            assert submissions.get(store, request_id)["status"] == "cancelled"
            assert [m["content"] for m in store.messages(session)] == ["Synthetic greeting"]
            assert len(payloads) == 1 and payloads[0]["stream"] is True
            with inference_control.local_capacity(provider.local_capacity_key, 0.1):
                pass
            # Replay doesn't call the provider again or append another user input.
            assert loop.run_turn(session, "Synthetic greeting", request_id=request_id)["replayed"]
            assert len(payloads) == 1
        finally:
            foreground_inference.cancel(store, request_id=request_id)
            server.shutdown()
            server.server_close()
            serving.join(30)
            worker.join(30)
    assert not foreground_inference._active


def test_scope_checks_stop_committed_before_registration(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        turn = store.start_turn(session)
        execution = session_settings.execution(store, session, turn)
        turn_control.cancel(store, turn)
        with (
            pytest.raises(ProviderUnavailable, match="stopped"),
            foreground_inference.scope(store, execution),
        ):
            pytest.fail("A prior stop must prevent provider entry")
    assert not foreground_inference._active


def test_stop_during_registration_cannot_miss_transport(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        turn = store.start_turn(session)
        execution = session_settings.execution(store, session, turn)
        original = turn_control.guard

        def stop_first(store, execution):
            turn_control.cancel(store, turn)
            original(store, execution)

        monkeypatch.setattr(turn_control, "guard", stop_first)
        with (
            pytest.raises(ProviderUnavailable, match="stopped"),
            foreground_inference.scope(store, execution),
        ):
            pytest.fail("A stop during registration must prevent provider entry")
    assert not foreground_inference._active


def test_late_completion_is_discarded_and_unrelated_call_remains_usable(tmp_path):
    tokens = []
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()

        class IgnoresStop(Recorder):
            def complete(self, messages, *, model):
                tokens.append(inference_control.current())
                turn_control.cancel_submission(store, "late_completion_request")
                return super().complete(messages, model=model)

        provider = IgnoresStop()
        with pytest.raises(TurnFailed, match="stopped"):
            loop_with(store, provider).run_turn(
                session, "First", request_id="late_completion_request"
            )
        assert len(provider.calls) == 1
        assert tokens[0].stopped.is_set()
        other = Recorder()
        result = loop_with(store, other).run_turn(
            session, "Next", request_id="next_completion_request"
        )
        assert result["message"]["content"] == "answered"
        assert len(other.calls) == 1
        assert [m["content"] for m in store.messages(session)] == ["First", "Next", "answered"]
    assert not foreground_inference._active


def test_cancellation_is_scoped_to_store_and_exact_turn(tmp_path):
    with closing(Store(tmp_path / "one.db")) as one, closing(Store(tmp_path / "two.db")) as two:
        session = one.create_session()
        turn = one.start_turn(session)
        execution = session_settings.execution(one, session, turn)
        with foreground_inference.scope(one, execution):
            token = inference_control.current()
            foreground_inference.cancel(two, turn_id=turn)
            foreground_inference.cancel(one, turn_id="unrelated")
            assert not token.stopped.is_set()
        foreground_inference.cancel(one, turn_id=turn)
        assert not token.stopped.is_set(), "Completed calls have no cancellation registration"


def test_preparation_scope_uses_submission_identity(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        request_id = "preparation_inference_stop"
        submissions.reserve(store, request_id, session, "Draft", {})
        execution = session_settings.execution(store, session, request_id=request_id)
        with (
            pytest.raises(ProviderUnavailable, match="stopped"),
            foreground_inference.scope(store, execution),
        ):
            token = inference_control.current()
            turn_control.cancel_submission(store, request_id)
            assert token.stopped.is_set()
        assert submissions.get(store, request_id)["status"] == "cancelled"
        assert store.messages(session) == []


def test_existing_background_token_keeps_its_original_owner(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        session = store.create_session()
        turn = store.start_turn(session)
        token = inference_control.Token()
        with (
            inference_control.bind(token, background=True),
            foreground_inference.scope(store, session_settings.execution(store, session, turn)),
        ):
            assert inference_control.current() is token
            assert not foreground_inference._active
        assert not token.stopped.is_set()


@pytest.mark.parametrize("new_stop", [False, True])
def test_delayed_old_stop_cannot_cancel_a_newly_consented_reply(tmp_path, monkeypatch, new_stop):
    from test_reply_recovery import stopped

    from pi.turns.failures import ActedWithoutReply

    delayed = []
    deliver = foreground_inference.cancel
    monkeypatch.setattr(foreground_inference, "cancel", lambda *a, **kw: delayed.append((a, kw)))
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, provider, gate, turn = stopped(store)
        assert len(delayed) == 1 and delayed[0][1]["requested_at"] is not None
        monkeypatch.setattr(foreground_inference, "cancel", deliver)
        original = provider.complete

        def during_recovery(messages, *, model):
            token = inference_control.current()
            deliver(*delayed[0][0], **delayed[0][1])
            assert not token.stopped.is_set(), "Earlier Stop cannot cancel later explicit consent"
            if new_stop:
                turn_control.cancel(store, turn)
                assert token.stopped.is_set(), "A new Stop must still interrupt this reply"
            return original(messages, model=model)

        provider.complete = during_recovery
        if new_stop:
            with pytest.raises(ActedWithoutReply):
                loop.recover_reply(turn, "new_explicit_reply_001")
            assert store.get_turn(turn)["status"] == "acted_no_reply"
        else:
            assert loop.recover_reply(turn, "new_explicit_reply_001")["status"] == "complete"
        assert len(gate.invocations) == 1 and len(provider.sent) == 2
    assert not foreground_inference._active
