"""The owner reaches a chat's context through the browser contract; turn snapshots stay with recovery.

The context writes themselves (policy, transfer, summary, compaction, items) are covered route by
route in tests/test_owner_chat_routes.py.
"""

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.api import browser_contract
from pi.api import server as api
from pi.conversation.store import Store

OWNER = "owner_context_test_key"


@pytest.fixture
def http(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
        monkeypatch.setattr(
            api.app.state,
            "owner_key_hash",
            hashlib.sha256(OWNER.encode()).hexdigest(),
            raising=False,
        )
        yield store, TestClient(api.app)


def test_owner_reads_context_and_summary(http):
    store, client = http
    session = store.create_session()
    owner = {"X-Pi-Owner-Key": OWNER}
    assert client.get(f"/context/{session}", headers=owner).status_code == 200
    assert client.get(f"/context/{session}/summary", headers=owner).status_code == 200
    assert client.get("/context/ses_missing", headers=owner).status_code == 404
    assert client.get(f"/context/{session}").status_code == 401


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("GET", "/turns/trn_1"),
        ("GET", "/selections/req_1"),
        ("GET", "/selections/compare_0123456789"),
        ("POST", "/items/dec_1/state"),
    ],
)
def test_owner_cannot_inspect_turns_or_use_a_malformed_id(http, method, suffix):
    store, client = http
    session = store.create_session()
    response = client.request(
        method, f"/context/{session}{suffix}", headers={"X-Pi-Owner-Key": OWNER}, json={}
    )
    assert response.status_code == 403


def test_recovery_keeps_full_access(http):
    store, client = http
    session = store.create_session()
    assert (
        client.get(f"/context/{session}", headers={"X-Pi-Key": "synthetic-admin"}).status_code
        == 200
    )


def test_contract_needs_a_real_chat_id_for_writes():
    allowed = browser_contract.owner_allowed
    assert allowed("GET", "/context/ses_abc") and allowed("GET", "/context/ses_abc/summary")
    assert not allowed("POST", "/context/ses_abc")
    assert allowed("POST", "/context/ses_0123456789abcdef")
    assert not allowed("POST", "/context/ses_0123456789ABCDEF")
    assert not allowed("POST", "/context/ses_0123456789abcdef/turns")
    assert not allowed("GET", "/context/ses_abc/turns/trn_1")
    assert not allowed("GET", "/context/ses_abc/selections/req_1")
    assert not browser_contract.runtime_allowed("GET", "/context/ses_abc")
