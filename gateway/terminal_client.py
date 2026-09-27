"""Single-channel Unix-socket client for the isolated owner-terminal sidecar."""

from __future__ import annotations

import base64
import json
import socket
import struct
import threading
from pathlib import Path

from pi.owner_terminal import TerminalError

MAX_REQUEST_BYTES = 16384
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _identity(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("terminal_") or len(value) != 41:
        raise TerminalError("Terminal sidecar returned an invalid session identity.")
    return value


def _receive(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise OSError("terminal channel closed")
        chunks.extend(chunk)
    return bytes(chunks)


class TerminalSidecar:
    """One connection for one sidecar lifetime; channel loss is never reconnected."""

    def __init__(self, path: str):
        candidate = Path(path)
        if not candidate.is_absolute():
            raise TerminalError("Terminal sidecar socket path must be absolute.")
        self.path = str(candidate)
        self.lock = threading.RLock()
        self.connection: socket.socket | None = None
        self.connection_attempted = False

    def _connect(self) -> socket.socket:
        if self.connection is not None:
            return self.connection
        if self.connection_attempted or not hasattr(socket, "AF_UNIX"):
            raise TerminalError("Terminal sidecar channel is unavailable and will not reconnect.")
        self.connection_attempted = True
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        try:
            connection.connect(self.path)
        except (OSError, TimeoutError):
            connection.close()
            raise TerminalError("Terminal sidecar is unavailable.") from None
        self.connection = connection
        return connection

    def _lost(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None

    def call(self, operation: str, **arguments) -> dict:
        request = json.dumps(
            {"operation": operation, "arguments": arguments},
            separators=(",", ":"),
        ).encode("utf-8")
        if len(request) > MAX_REQUEST_BYTES:
            raise TerminalError("Terminal sidecar request is too large.")
        with self.lock:
            connection = self._connect()
            try:
                connection.sendall(struct.pack("!I", len(request)) + request)
                size = struct.unpack("!I", _receive(connection, 4))[0]
                if size > MAX_RESPONSE_BYTES:
                    raise OSError("oversized terminal response")
                encoded = _receive(connection, size)
            except (OSError, TimeoutError):
                self._lost()
                raise TerminalError(
                    "Terminal sidecar channel was lost; leases will not be recreated."
                ) from None
        try:
            response = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._lost()
            raise TerminalError("Terminal sidecar returned an unreadable response.") from None
        if not isinstance(response, dict) or set(response) != {"ok", "result"}:
            self._lost()
            raise TerminalError("Terminal sidecar returned an invalid response.")
        if response["ok"] is False and isinstance(response["result"], str):
            raise TerminalError(response["result"])
        if response["ok"] is not True or not isinstance(response["result"], dict):
            self._lost()
            raise TerminalError("Terminal sidecar returned an invalid response.")
        return response["result"]

    def readiness(self) -> dict:
        result = self.call("readiness")
        if set(result) != {"status", "active", "capacity", "secretsIncluded"}:
            raise TerminalError("Terminal sidecar readiness is invalid.")
        if (
            result["status"] != "ready"
            or type(result["active"]) is not int
            or not 0 <= result["active"] <= 1
            or result["capacity"] != 1
            or result["secretsIncluded"] is not False
        ):
            raise TerminalError("Terminal sidecar is not ready.")
        return result

    def close(self) -> None:
        with self.lock:
            self._lost()


class RemoteTerminal:
    """Terminal-compatible proxy; the PTY and child process stay in the sidecar."""

    def __init__(
        self,
        sidecar: TerminalSidecar,
        workspace_label: str,
        request_identity: str,
    ):
        del workspace_label
        self.sidecar = sidecar
        result = self.sidecar.call("create", requestId=request_identity)
        if set(result) != {"id", "replayed"} or type(result["replayed"]) is not bool:
            raise TerminalError("Terminal sidecar creation response is invalid.")
        self.identity = _identity(result["id"])
        self._closed = False

    @property
    def closed(self) -> bool:
        if self._closed:
            return True
        try:
            result = self.sidecar.call("status", id=self.identity)
        except TerminalError:
            self._closed = True
            return True
        if set(result) != {"closed"} or type(result["closed"]) is not bool:
            raise TerminalError("Terminal sidecar status is invalid.")
        self._closed = result["closed"]
        return self._closed

    def write(self, data: bytes) -> int:
        if not isinstance(data, bytes) or not 1 <= len(data) <= 8192:
            raise TerminalError("Terminal input must contain 1-8192 bytes.")
        result = self.sidecar.call(
            "write", id=self.identity, data=base64.b64encode(data).decode("ascii")
        )
        if set(result) != {"acceptedBytes"} or result["acceptedBytes"] != len(data):
            raise TerminalError("Terminal sidecar did not confirm the exact input.")
        return result["acceptedBytes"]

    def resize(self, rows: int, columns: int) -> None:
        result = self.sidecar.call("resize", id=self.identity, rows=rows, columns=columns)
        if result != {"resized": True}:
            raise TerminalError("Terminal sidecar did not confirm resize.")

    def read(self, cursor: int = 0) -> dict:
        result = self.sidecar.call("read", id=self.identity, cursor=cursor)
        if set(result) != {"data", "cursor", "droppedBytes", "exitCode"}:
            raise TerminalError("Terminal sidecar output is invalid.")
        try:
            data = base64.b64decode(result["data"], validate=True)
        except (TypeError, ValueError):
            raise TerminalError("Terminal sidecar output is invalid.") from None
        if (
            len(data) > 1024 * 1024
            or type(result["cursor"]) is not int
            or result["cursor"] < 0
            or type(result["droppedBytes"]) is not int
            or result["droppedBytes"] < 0
            or (result["exitCode"] is not None and type(result["exitCode"]) is not int)
        ):
            raise TerminalError("Terminal sidecar output is invalid.")
        return {**result, "data": data}

    def close(self) -> None:
        if self._closed:
            return
        result = self.sidecar.call("close", id=self.identity)
        if result != {"closed": True}:
            raise TerminalError("Terminal sidecar did not confirm closure.")
        self._closed = True
