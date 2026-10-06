from contextlib import ExitStack

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.api import COOKIE, Config, create_app
from gateway.store import AuthStore

PASSWORD = "disposable test owner passphrase"


def test_two_ports_share_a_cookie_jar_without_replacing_sessions(tmp_path):
    with ExitStack() as stack:
        clients = []
        configs = []
        for port, namespace in [(8443, ""), (8444, "qa-20261006")]:
            auth = AuthStore(tmp_path / f"auth-{port}.db")
            auth.set_password(PASSWORD)
            config = Config(
                f"https://localhost:{port}",
                str(auth.path),
                "http://pi:8050",
                "separate-runtime-key-" + "r" * 32,
                cookie_namespace=namespace,
            )
            configs.append(config)
            clients.append(
                stack.enter_context(
                    TestClient(
                        create_app(
                            config,
                            store=auth,
                            transport=httpx.MockTransport(
                                lambda request: httpx.Response(200, json={"results": []}),
                            ),
                        ),
                        base_url=config.origin,
                    )
                )
            )
        main, qa = clients
        qa.cookies.jar = main.cookies.jar
        identities = []
        csrf = []
        for client, config in zip(clients, configs, strict=True):
            anonymous = client.get("/auth/session")
            header = anonymous.headers["set-cookie"]
            assert header.startswith(config.cookie_name + "=")
            assert all(
                flag in header for flag in ["Secure", "HttpOnly", "SameSite=strict", "Path=/"]
            )
            assert "Domain=" not in header
            response = client.post(
                "/auth/login",
                json={"password": PASSWORD},
                headers={
                    "Origin": config.origin,
                    "X-CSRF-Token": anonymous.json()["csrf_token"],
                },
            )
            assert response.status_code == 200
            identities.append(response.json()["session_id"])
            csrf.append(response.json()["csrf_token"])
        assert configs[0].cookie_name == COOKIE
        assert configs[1].cookie_name != COOKIE
        assert identities[0] != identities[1]
        for client, identity in zip(clients, identities, strict=True):
            current = client.get("/auth/session").json()
            assert current["authenticated"]
            assert current["session_id"] == identity
            assert client.get("/api/pi/sessions").status_code == 200
        # A main-session CSRF token cannot log out the QA session.
        assert (
            qa.post(
                "/auth/logout",
                headers={
                    "Origin": configs[1].origin,
                    "X-CSRF-Token": csrf[0],
                },
            ).status_code
            == 403
        )
        old_qa = qa.cookies.get(configs[1].cookie_name)
        assert (
            qa.post(
                "/auth/logout",
                headers={
                    "Origin": configs[1].origin,
                    "X-CSRF-Token": csrf[1],
                },
            ).status_code
            == 200
        )
        assert main.get("/auth/session").json()["session_id"] == identities[0]
        assert main.get("/api/pi/sessions").status_code == 200
        assert qa.get("/api/pi/sessions").status_code == 401, "No fallback to the main cookie"
        assert (
            qa.get(
                "/api/pi/sessions",
                headers={
                    "Cookie": f"{configs[1].cookie_name}={old_qa}",
                },
            ).status_code
            == 401
        ), "Namespaced logout still revokes the server-side token"
        assert not qa.get("/auth/session").json()["authenticated"]
        assert main.get("/auth/session").json()["authenticated"]


@pytest.mark.parametrize("namespace", ["UPPER", "bad_name", "bad;name", "../qa", "qa\n", "x" * 41])
def test_invalid_cookie_namespace_fails_closed(namespace):
    config = Config(
        "https://localhost", "/unused", "http://pi:8050", "r" * 40, cookie_namespace=namespace
    )
    with pytest.raises(ValueError, match="GATEWAY_COOKIE_NAMESPACE"):
        config.validate()


def test_cookie_namespace_is_loaded_from_host_configuration(monkeypatch):
    monkeypatch.setenv("GATEWAY_COOKIE_NAMESPACE", "qa-20261006")
    assert Config.environment().cookie_name == "__Host-conker-qa-20261006"
