"""Real Linux PTY through owner gateway; temporary files and synthetic commands only."""

import base64
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.api import Config, create_app
from gateway.store import AuthStore
from pi.owner_terminal import Terminal

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Requires Linux controlling PTY")


def test_authenticated_gateway_controls_real_pty_and_revocation(tmp_path, monkeypatch):
    origin = "https://localhost:8050"
    password = "synthetic owner terminal acceptance phrase"
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(password, initial=True)
    monkeypatch.setenv("CONKER_SYNTHETIC_SECRET", "synthetic-do-not-inherit")
    spawned = []

    def factory(shell, directory):
        terminal = Terminal(shell, directory, lifetime=30)
        spawned.append(terminal)
        return terminal

    config = Config(
        origin,
        str(auth.path),
        "http://pi.invalid",
        "x" * 32,
        terminal_shell="/bin/bash",
        terminal_directory=str(tmp_path),
    )

    def upstream(request):
        pytest.fail("Terminal must not call Pi or ToolGate")

    app = create_app(
        config, store=auth, transport=httpx.MockTransport(upstream), terminal_factory=factory
    )
    with TestClient(app, base_url=origin) as client:
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post(
            "/auth/login",
            json={"password": password},
            headers={"Origin": origin, "X-CSRF-Token": csrf},
        )
        assert login.status_code == 200
        headers = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}
        body = {"requestId": "linux_terminal_contract"}
        assert client.post("/api/terminal", json=body, headers=headers).status_code == 428
        assert not spawned
        verified = client.post(
            "/auth/verify",
            headers=headers,
            json={
                "password": password,
                "operation": {"method": "POST", "path": "/api/terminal", "body": body},
            },
        )
        assert verified.status_code == 200
        proof = {**headers, "X-Conker-Verification": verified.json()["verification_token"]}
        assert client.post("/api/terminal", json=body, headers=proof).status_code == 200
        assert client.post("/api/terminal", json=body, headers=headers).json()["replayed"]
        assert len(spawned) == 1
        path = "/api/terminal/" + body["requestId"]
        assert (
            client.post(
                path + "/resize", headers=headers, json={"rows": 28, "columns": 94}
            ).status_code
            == 200
        )
        command = (
            b"printf 'gateway-%s\\n' 'pty-ok'; printf 'isolated=%s\\n' "
            b'"${CONKER_SYNTHETIC_SECRET-unset}"; stty size\n'
        )
        payload = {"data": base64.b64encode(command).decode()}
        assert client.post(path + "/input", json=payload).status_code == 403
        sent = client.post(path + "/input", headers=headers, json=payload)
        assert sent.json()["acceptedBytes"] == len(command)
        cursor, output = 0, bytearray()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get(path, params={"cursor": cursor})
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            result = response.json()
            output.extend(base64.b64decode(result["data"]))
            cursor = result["cursor"]
            if b"gateway-pty-ok" in output and b"28 94" in output:
                break
            time.sleep(0.02)
        assert b"gateway-pty-ok" in output and b"28 94" in output
        assert b"isolated=unset" in output
        assert b"synthetic-do-not-inherit" not in output
        assert b"no job control" not in output
        auth.revoke()
        deadline = time.monotonic() + 3
        while not spawned[0].closed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert spawned[0].closed
        assert spawned[0].process.poll() is not None
        assert client.get(path).status_code == 401
    assert spawned[0].closed
