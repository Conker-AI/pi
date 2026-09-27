"""Probe normal and hostile owner-terminal container behavior."""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import socket
import struct
import time
from pathlib import Path


def receive(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise RuntimeError("sidecar channel closed")
        result.extend(chunk)
    return bytes(result)


def call(connection: socket.socket, operation: str, **arguments) -> dict:
    request = json.dumps(
        {"operation": operation, "arguments": arguments}, separators=(",", ":")
    ).encode("utf-8")
    connection.sendall(struct.pack("!I", len(request)) + request)
    size = struct.unpack("!I", receive(connection, 4))[0]
    response = json.loads(receive(connection, size).decode("utf-8"))
    if response.get("ok") is not True or not isinstance(response.get("result"), dict):
        raise RuntimeError(f"sidecar rejected {operation}: {response!r}")
    return response["result"]


def write(connection: socket.socket, identity: str, command: bytes) -> None:
    assert call(
        connection,
        "write",
        id=identity,
        data=base64.b64encode(command).decode("ascii"),
    ) == {"acceptedBytes": len(command)}


def read_until(
    connection: socket.socket,
    identity: str,
    expected: tuple[bytes, ...],
    *,
    timeout: float = 5,
) -> bytes:
    output = bytearray()
    cursor = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = call(connection, "read", id=identity, cursor=cursor)
        output.extend(base64.b64decode(result["data"], validate=True))
        cursor = result["cursor"]
        if all(marker in output for marker in expected):
            return bytes(output)
        time.sleep(0.05)
    raise AssertionError(f"terminal output lacked expected markers: {expected!r}")


def normal_probe(connection: socket.socket, path: Path, identity: str) -> None:
    call(connection, "resize", id=identity, rows=31, columns=97)
    command = (
        b"printf 'container-pty-ok\\n'; stty size; "
        b"if env | grep -Eq '^(PI_|GATEWAY_|TOOLGATE_|MEMORYGATE_|OPENAI_|"
        b"ANTHROPIC_|OPENROUTER_)'; then printf 'boundary-failed\\n'; "
        b"elif [ -e /run/secrets ] || [ -e /auth ] || [ -S /var/run/docker.sock ]; "
        b"then printf 'boundary-failed\\n'; "
        b"elif [ \"$(wc -l < /proc/net/route)\" -ne 1 ]; "
        b"then printf 'boundary-failed\\n'; else printf 'boundary-ok\\n'; fi; "
        b"setsid -f /bin/sh -c 'printf escaped-descendant-ready\\n; sleep 300' &\n"
    )
    write(connection, identity, command)
    read_until(
        connection,
        identity,
        (
            b"container-pty-ok",
            b"31 97",
            b"boundary-ok",
            b"escaped-descendant-ready",
        ),
    )
    assert call(connection, "close", id=identity) == {"closed": True}
    assert call(connection, "readiness")["active"] == 0
    assert not path.exists(), "accepted listener path must remain unlinked"


def attack_probe(connection: socket.socket, path: Path, identity: str) -> None:
    fake_listener = """
import pathlib
import socket

path = "/run/conker-terminal/control.sock"
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(path)
listener.listen(1)
pathlib.Path("/workspace/fake-listener-ready").write_text("ready", encoding="ascii")
listener.settimeout(20)
try:
    accepted, _ = listener.accept()
except TimeoutError:
    pass
else:
    pathlib.Path("/workspace/fake-listener-accepted").write_text(
        "accepted", encoding="ascii"
    )
    accepted.close()
""".strip()
    command = (
        f"setsid -f python -c {shlex.quote(fake_listener)}; "
        "for attempt in $(seq 1 100); do "
        "[ -S /run/conker-terminal/control.sock ] && break; sleep 0.02; done; "
        "[ -S /run/conker-terminal/control.sock ] && printf 'fake-listener-ready\\n'\n"
    ).encode()
    write(connection, identity, command)
    read_until(connection, identity, (b"fake-listener-ready",))
    assert path.exists(), "PTY did not establish the hostile replacement listener"
    assert call(connection, "readiness")["status"] == "ready"

    terminate = (
        "python -c 'import json,os,signal,time; time.sleep(0.3); "
        "pid=json.load(open(\"/run/conker-terminal/health.json\"))[\"pid\"]; "
        "os.kill(pid,signal.SIGTERM)' &\n"
    ).encode("ascii")
    write(connection, identity, terminate)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            call(connection, "readiness")
        except (OSError, RuntimeError):
            break
        time.sleep(0.05)
    else:
        raise AssertionError("supervisor termination did not close the gateway channel")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket", type=Path)
    parser.add_argument("--mode", choices=("normal", "attack"), default="normal")
    arguments = parser.parse_args()
    path = arguments.socket
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(str(path))
    with connection:
        assert call(connection, "readiness") == {
            "status": "ready",
            "active": 0,
            "capacity": 1,
            "secretsIncluded": False,
        }
        created = call(connection, "create", requestId="container_probe_request_01")
        identity = created["id"]
        assert created["replayed"] is False
        assert not path.exists(), "accepted listener path must be unlinked"
        write(connection, identity, b"stty -echo\n")
        time.sleep(0.1)
        if arguments.mode == "normal":
            normal_probe(connection, path, identity)
        else:
            attack_probe(connection, path, identity)
    print(f"owner-terminal container {arguments.mode} probe passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
