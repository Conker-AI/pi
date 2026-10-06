from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

import pi.terminal_sidecar as terminal_sidecar
from gateway.terminal_client import RemoteTerminal, TerminalSidecar
from pi.owner_terminal import TerminalError
from pi.terminal_sidecar import TerminalSessions, health, serve


class FakeTerminal:
    def __init__(self, shell, workspace):
        self.shell, self.workspace = shell, workspace
        self.closed = False
        self.buffer = bytearray()
        self.size = None

    def write(self, value):
        if self.closed:
            raise TerminalError("Terminal session expired or closed.")
        self.buffer.extend(value)
        return len(value)

    def read(self, cursor=0):
        if self.closed:
            raise TerminalError("Terminal session expired or closed.")
        if type(cursor) is not int or not 0 <= cursor <= len(self.buffer):
            raise TerminalError("Invalid output cursor.")
        return {
            "data": bytes(self.buffer[cursor:]),
            "cursor": len(self.buffer),
            "droppedBytes": 0,
            "exitCode": None,
        }

    def resize(self, rows, columns):
        if not 2 <= rows <= 200 or not 10 <= columns <= 400:
            raise TerminalError("Invalid terminal dimensions.")
        self.size = (rows, columns)

    def close(self):
        self.closed = True


@pytest.fixture
def connected_sidecar():
    if not hasattr(socket, "AF_UNIX") or not hasattr(socket, "SO_PEERCRED"):
        pytest.skip("Linux Unix peer credentials are unavailable")
    path = Path(tempfile.gettempdir()) / f"conker-{uuid.uuid4().hex}.sock"
    health_path = path.with_suffix(".json")
    sessions = TerminalSessions("/bin/bash", "/workspace", factory=FakeTerminal)
    thread = threading.Thread(
        target=serve,
        args=(path, sessions, os.getuid()),
        kwargs={"install_signal_handlers": False, "health_path": health_path},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    # The socket exists after bind(), before listen(). The heartbeat starts
    # only after listen(), without consuming the single accepted connection.
    while not health(health_path) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert health(health_path), "sidecar did not finish listening"
    client = TerminalSidecar(str(path))
    assert client.readiness()["status"] == "ready"
    try:
        yield client, sessions, path
    finally:
        client.close()
        thread.join(timeout=5)
        path.unlink(missing_ok=True)
        health_path.unlink(missing_ok=True)
        assert not thread.is_alive()


def test_dispatch_creation_is_idempotent_and_bounded():
    synthetic_path = str(Path.cwd())
    sessions = TerminalSessions(synthetic_path, synthetic_path, factory=FakeTerminal)
    first = sessions.dispatch("create", {"requestId": "terminal_request_01"})
    replay = sessions.dispatch("create", {"requestId": "terminal_request_01"})
    assert first["replayed"] is False
    assert replay == {"id": first["id"], "replayed": True}
    assert len(sessions.entries) == 1
    with pytest.raises(TerminalError, match="Invalid terminal request"):
        sessions.dispatch("create", {"requestId": "short"})
    sessions.close()


def test_remote_terminal_round_trip_stays_bounded_and_secret_free(connected_sidecar):
    client, sessions, path = connected_sidecar
    terminal = RemoteTerminal(client, "Owner workspace", "terminal_request_01")
    assert not path.exists(), "accepted channel must unlink the listener path"
    assert terminal.write(b"synthetic input") == 15
    assert terminal.read(0) == {
        "data": b"synthetic input",
        "cursor": 15,
        "droppedBytes": 0,
        "exitCode": None,
    }
    terminal.resize(28, 94)
    saved = sessions.entries[terminal.identity]
    assert saved.shell == "/bin/bash" and saved.workspace == "/workspace"
    assert saved.size == (28, 94)
    terminal.close()
    assert terminal.closed


def test_sidecar_enforces_capacity_and_returns_only_static_errors(connected_sidecar):
    client, _sessions, _path = connected_sidecar
    terminals = [RemoteTerminal(client, "Owner workspace", "terminal_request_00")]
    with pytest.raises(TerminalError, match="session limit") as failure:
        RemoteTerminal(client, "Owner workspace", "terminal_request_99")
    assert "workspace" not in str(failure.value).lower()
    for terminal in terminals:
        terminal.close()


def test_cleanup_failure_permanently_fails_readiness():
    synthetic_path = str(Path.cwd())
    sessions = TerminalSessions(synthetic_path, synthetic_path, factory=FakeTerminal)
    sessions.cleanup_failed = True

    with pytest.raises(TerminalError, match="recreate the sidecar"):
        sessions.dispatch("readiness", {})
    with pytest.raises(TerminalError, match="recreate the sidecar"):
        sessions.dispatch("create", {"requestId": "terminal_request_01"})


def test_isolated_cleanup_kills_every_peer_uid_process(monkeypatch):
    synthetic_path = str(Path.cwd())
    sessions = TerminalSessions(
        synthetic_path,
        synthetic_path,
        factory=FakeTerminal,
        isolated_uid_cleanup=True,
    )
    observed = iter([[120, 121], []])
    monkeypatch.setattr(sessions, "_peer_processes", lambda: next(observed))
    killed = []
    monkeypatch.setattr(os, "kill", lambda process, signal: killed.append(process))
    monkeypatch.setattr(time, "sleep", lambda _duration: None)

    sessions._cleanup_uid_processes()

    assert killed == [120, 121]
    assert sessions.cleanup_failed is False


def test_isolated_cleanup_failure_requires_recreation(monkeypatch):
    synthetic_path = str(Path.cwd())
    sessions = TerminalSessions(
        synthetic_path,
        synthetic_path,
        factory=FakeTerminal,
        isolated_uid_cleanup=True,
    )
    monkeypatch.setattr(sessions, "_peer_processes", lambda: [120])
    monkeypatch.setattr(os, "kill", lambda _process, _signal: None)
    monkeypatch.setattr(time, "sleep", lambda _duration: None)

    with pytest.raises(TerminalError, match="could not be proven"):
        sessions._cleanup_uid_processes()

    assert sessions.cleanup_failed is True


def test_health_requires_fresh_ready_live_supervisor(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    payload = {
        "schemaVersion": 1,
        "pid": os.getpid(),
        "ready": True,
        "updatedMonotonicNs": time.monotonic_ns(),
    }

    monkeypatch.setattr(terminal_sidecar, "_process_alive", lambda _pid: True)
    path.write_text(json.dumps(payload), encoding="ascii")
    assert health(path)

    payload["ready"] = False
    path.write_text(json.dumps(payload), encoding="ascii")
    assert not health(path)

    payload["ready"] = True
    payload["updatedMonotonicNs"] = time.monotonic_ns() - 10_000_000_000
    path.write_text(json.dumps(payload), encoding="ascii")
    assert not health(path)

    payload["updatedMonotonicNs"] = time.monotonic_ns()
    path.write_text(json.dumps(payload), encoding="ascii")

    monkeypatch.setattr(terminal_sidecar, "_process_alive", lambda _pid: False)
    assert not health(path)


def test_client_never_reconnects_after_channel_loss(connected_sidecar):
    client, _sessions, _path = connected_sidecar
    client.close()
    with pytest.raises(TerminalError, match="will not reconnect"):
        client.readiness()


def test_client_rejects_relative_or_missing_socket():
    with pytest.raises(TerminalError, match="absolute"):
        TerminalSidecar("relative.sock")
    missing = Path(tempfile.gettempdir()) / f"missing-{os.getpid()}-{uuid.uuid4().hex}.sock"
    with pytest.raises(TerminalError, match="unavailable"):
        TerminalSidecar(str(missing)).readiness()
