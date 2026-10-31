"""Pi refuses to start when two of its credentials are the same key (pi/api/startup.py)."""

import asyncio
import hashlib
import os

import httpx
import pytest

from pi.api import server as api
from pi.api import startup

ADMIN, GATEWAY, OWNER = "synthetic-admin-key-only", "gateway-key-synthetic", "owner-key-synthetic"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def start(monkeypatch, tmp_path, gateway, owner):
    for name in list(os.environ):
        if name.startswith("PI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("PI_ADMIN_KEY", ADMIN)
    monkeypatch.setenv("PI_GATEWAY_KEY_SHA256", gateway)
    monkeypatch.setenv("PI_OWNER_KEY_SHA256", owner)
    monkeypatch.setenv("PI_DB_PATH", str(tmp_path / "startup.db"))

    def no_network(*args, **kwargs):
        pytest.fail("Startup must not contact external services")

    monkeypatch.setattr(httpx, "post", no_network)
    monkeypatch.setattr(httpx, "get", no_network)
    seen = {}

    async def run():
        async with startup.lifespan(api.app):
            seen["gateway"] = api.app.state.gateway_key_hash
            seen["owner"] = api.app.state.owner_key_hash

    asyncio.run(run())
    return seen


def test_startup_accepts_distinct_credentials(monkeypatch, tmp_path):
    seen = start(monkeypatch, tmp_path, digest(GATEWAY), digest(OWNER))
    assert seen == {"gateway": digest(GATEWAY), "owner": digest(OWNER)}


@pytest.mark.parametrize(
    ("gateway", "owner"),
    [
        # The admin key is recovery authority; the gateway must not carry it.
        (digest(ADMIN), digest(OWNER)),
        (digest(ADMIN), ""),
        (digest(GATEWAY), digest(GATEWAY)),
        (digest(GATEWAY), digest(ADMIN)),
    ],
)
def test_startup_refuses_a_reused_credential(monkeypatch, tmp_path, gateway, owner):
    with pytest.raises(RuntimeError):
        start(monkeypatch, tmp_path, gateway, owner)
    # Refused before anything was opened.
    assert not (tmp_path / "startup.db").exists()
