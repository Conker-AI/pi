"""Networkless owner-terminal supervisor with one UID-bound gateway channel."""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import secrets
import signal
import socket
import struct
import threading
import time
from pathlib import Path

from .owner_terminal import Terminal, TerminalError

MAX_REQUEST_BYTES = 16384
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{16,100}")
KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
HEALTH_SCHEMA = 1
HEALTH_INTERVAL_SECONDS = 1.0
HEALTH_MAX_AGE_SECONDS = 5.0


def _receive(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise OSError("terminal channel closed")
        chunks.extend(chunk)
    return bytes(chunks)


class TerminalSessions:
    def __init__(
        self,
        shell: str,
        workspace: str,
        *,
        factory=Terminal,
        isolated_uid_cleanup: bool = False,
    ):
        if not Path(shell).is_absolute() or not Path(workspace).is_absolute():
            raise TerminalError("Terminal sidecar requires absolute shell and workspace paths.")
        self.shell, self.workspace, self.factory = shell, workspace, factory
        self.isolated_uid_cleanup = isolated_uid_cleanup
        self.lock = threading.RLock()
        self.entries: dict[str, Terminal] = {}
        self.requests: dict[str, str] = {}
        self.cleanup_failed = False

    @staticmethod
    def _peer_processes() -> list[int]:
        own_uid = os.getuid()
        excluded = {1, os.getpid()}
        processes = []
        for candidate in Path("/proc").iterdir():
            if not candidate.name.isdigit() or int(candidate.name) in excluded:
                continue
            try:
                status = (candidate / "status").read_text(encoding="ascii")
            except (FileNotFoundError, PermissionError, ProcessLookupError, UnicodeError):
                continue
            fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
            if fields.get("State", "").lstrip().startswith("Z"):
                continue
            uids = fields.get("Uid", "").split()
            if uids and int(uids[0]) == own_uid:
                processes.append(int(candidate.name))
        return processes

    def _cleanup_uid_processes(self) -> None:
        if not self.isolated_uid_cleanup:
            return
        for _attempt in range(40):
            processes = self._peer_processes()
            if not processes:
                return
            for process in processes:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(process, KILL_SIGNAL)
            time.sleep(0.025)
        self.cleanup_failed = True
        raise TerminalError("Terminal process cleanup could not be proven; recreate the sidecar.")

    def _create_terminal(self):
        if self.factory is Terminal:
            return Terminal(
                self.shell,
                self.workspace,
                cleanup=self._cleanup_uid_processes,
            )
        return self.factory(self.shell, self.workspace)

    def _terminal(self, identity: object):
        if not isinstance(identity, str):
            raise TerminalError("Terminal session is unavailable.")
        terminal = self.entries.get(identity)
        if terminal is None:
            raise TerminalError("Terminal session is unavailable.")
        return terminal

    def dispatch(self, operation: object, arguments: object) -> dict:
        if not isinstance(operation, str) or not isinstance(arguments, dict):
            raise TerminalError("Invalid terminal sidecar request.")
        with self.lock:
            if operation == "readiness" and not arguments:
                if self.cleanup_failed:
                    raise TerminalError(
                        "Terminal process cleanup is unproven; recreate the sidecar."
                    )
                active = sum(not terminal.closed for terminal in self.entries.values())
                return {
                    "status": "ready",
                    "active": active,
                    "capacity": 1,
                    "secretsIncluded": False,
                }
            if operation == "create" and set(arguments) == {"requestId"}:
                request_id = arguments["requestId"]
                if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
                    raise TerminalError("Invalid terminal request identity.")
                if request_id in self.requests:
                    return {"id": self.requests[request_id], "replayed": True}
                if len(self.requests) >= 1000:
                    raise TerminalError("Terminal request ledger is full.")
                if self.cleanup_failed:
                    raise TerminalError(
                        "Terminal process cleanup is unproven; recreate the sidecar."
                    )
                if sum(not terminal.closed for terminal in self.entries.values()) >= 1:
                    raise TerminalError("Terminal session limit reached.")
                identity = "terminal_" + secrets.token_hex(16)
                self.entries[identity] = self._create_terminal()
                self.requests[request_id] = identity
                return {"id": identity, "replayed": False}
            if set(arguments) - {"id", "data", "cursor", "rows", "columns"}:
                raise TerminalError("Invalid terminal sidecar request.")
            terminal = self._terminal(arguments.get("id"))
            if operation == "status" and set(arguments) == {"id"}:
                return {"closed": terminal.closed}
            if operation == "read" and set(arguments) == {"id", "cursor"}:
                result = terminal.read(arguments["cursor"])
                return {
                    **result,
                    "data": base64.b64encode(result["data"]).decode("ascii"),
                }
            if operation == "write" and set(arguments) == {"id", "data"}:
                try:
                    data = base64.b64decode(arguments["data"], validate=True)
                except (TypeError, ValueError):
                    raise TerminalError("Invalid terminal sidecar input.") from None
                return {"acceptedBytes": terminal.write(data)}
            if operation == "resize" and set(arguments) == {"id", "rows", "columns"}:
                terminal.resize(arguments["rows"], arguments["columns"])
                return {"resized": True}
            if operation == "close" and set(arguments) == {"id"}:
                terminal.close()
                return {"closed": True}
            raise TerminalError("Invalid terminal sidecar request.")

    def close(self) -> None:
        with self.lock:
            for terminal in self.entries.values():
                terminal.close()


class HealthReporter:
    """Ephemeral supervisor liveness; never inspects a shell or workspace."""

    def __init__(self, path: Path, sessions: TerminalSessions):
        if not path.is_absolute():
            raise TerminalError("Terminal health path must be absolute.")
        self.path = path
        self.sessions = sessions
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _write(self) -> None:
        payload = {
            "schemaVersion": HEALTH_SCHEMA,
            "pid": os.getpid(),
            "ready": not self.sessions.cleanup_failed,
            "updatedMonotonicNs": time.monotonic_ns(),
        }
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="ascii")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            self._write()
            self.stop_event.wait(HEALTH_INTERVAL_SECONDS)

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise TerminalError("Terminal health path is unsafe.")
        self.path.unlink(missing_ok=True)
        self._write()
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        self.path.unlink(missing_ok=True)


def health(path: Path, *, max_age: float = HEALTH_MAX_AGE_SECONDS) -> bool:
    """Return true only for a fresh, ready heartbeat from a live supervisor."""

    if not path.is_absolute() or path.is_symlink() or max_age <= 0:
        return False
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return False
    if not isinstance(payload, dict) or set(payload) != {
        "schemaVersion",
        "pid",
        "ready",
        "updatedMonotonicNs",
    }:
        return False
    pid = payload["pid"]
    updated = payload["updatedMonotonicNs"]
    if (
        payload["schemaVersion"] != HEALTH_SCHEMA
        or type(pid) is not int
        or pid <= 1
        or payload["ready"] is not True
        or type(updated) is not int
        or updated > time.monotonic_ns()
        or time.monotonic_ns() - updated > int(max_age * 1_000_000_000)
    ):
        return False
    return _process_alive(pid)


def _process_alive(pid: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _response(sessions: TerminalSessions, encoded: bytes) -> bytes:
    try:
        request = json.loads(encoded.decode("utf-8"))
        if not isinstance(request, dict) or set(request) != {"operation", "arguments"}:
            raise TerminalError("Invalid terminal sidecar request.")
        result = sessions.dispatch(request["operation"], request["arguments"])
        response = {"ok": True, "result": result}
    except TerminalError as exc:
        response = {"ok": False, "result": str(exc)}
    except (UnicodeDecodeError, ValueError, TypeError):
        response = {"ok": False, "result": "Invalid terminal sidecar request."}
    except Exception:
        response = {"ok": False, "result": "Terminal sidecar operation failed."}
    result = json.dumps(response, separators=(",", ":")).encode("utf-8")
    if len(result) > MAX_RESPONSE_BYTES:
        return json.dumps(
            {"ok": False, "result": "Terminal sidecar response is too large."},
            separators=(",", ":"),
        ).encode("utf-8")
    return result


def _peer_uid(connection: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise TerminalError("Terminal sidecar requires Linux peer credentials.")
    credentials = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid


def serve(
    socket_path: Path,
    sessions: TerminalSessions,
    expected_gateway_uid: int,
    *,
    health_path: Path | None = None,
    install_signal_handlers: bool = True,
) -> None:
    if not hasattr(socket, "AF_UNIX"):
        raise TerminalError("Terminal sidecar requires Unix sockets.")
    if not socket_path.is_absolute() or expected_gateway_uid < 0:
        raise TerminalError("Terminal sidecar configuration is invalid.")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.is_symlink() or (socket_path.exists() and not socket_path.is_socket()):
        raise TerminalError("Terminal sidecar socket path is unsafe.")
    socket_path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stop = threading.Event()
    reporter = HealthReporter(health_path, sessions) if health_path is not None else None

    def request_stop(signum, frame):
        del signum, frame
        stop.set()
        listener.close()

    if install_signal_handlers:
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
    try:
        listener.bind(str(socket_path))
        os.chmod(socket_path, 0o660)
        listener.listen(4)
        if reporter is not None:
            reporter.start()
        connection = None
        while not stop.is_set():
            try:
                candidate, _address = listener.accept()
            except OSError:
                if stop.is_set():
                    return
                raise
            if _peer_uid(candidate) == expected_gateway_uid:
                connection = candidate
                break
            candidate.close()
        if connection is None:
            return
        listener.close()
        socket_path.unlink(missing_ok=True)
        with connection:
            connection.settimeout(None)
            while not stop.is_set():
                try:
                    size = struct.unpack("!I", _receive(connection, 4))[0]
                    if size > MAX_REQUEST_BYTES:
                        raise OSError("oversized terminal request")
                    response = _response(sessions, _receive(connection, size))
                    connection.sendall(struct.pack("!I", len(response)) + response)
                except OSError:
                    break
    finally:
        listener.close()
        sessions.close()
        if reporter is not None:
            reporter.close()
        socket_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve_command = commands.add_parser("serve")
    serve_command.add_argument("--socket", type=Path, required=True)
    serve_command.add_argument("--health", type=Path, required=True)
    serve_command.add_argument("--shell", required=True)
    serve_command.add_argument("--workspace", required=True)
    serve_command.add_argument("--gateway-uid", type=int, required=True)
    health_command = commands.add_parser("health")
    health_command.add_argument("--health", type=Path, required=True)
    arguments = parser.parse_args(argv)
    if arguments.command == "health":
        return 0 if health(arguments.health) else 1
    if arguments.gateway_uid == os.getuid():
        raise SystemExit("Terminal sidecar UID must differ from the gateway UID.")
    if os.environ.get("CONKER_TERMINAL_ISOLATED") != "1":
        raise SystemExit("Terminal sidecar requires its isolated container contract.")
    sessions = TerminalSessions(
        arguments.shell,
        arguments.workspace,
        isolated_uid_cleanup=True,
    )
    serve(
        arguments.socket,
        sessions,
        arguments.gateway_uid,
        health_path=arguments.health,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
