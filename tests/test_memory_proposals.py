import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import httpx
import pytest

from pi import agents, forgetting, session_settings
from pi import memory_proposals as p
from pi.memory_corrections import Client
from pi.store import Store


class Remote:
    def __init__(self):
        self.text, self.revision = "Old preference", 3
        self.receipts, self.writes = {}, []
        self.lose_response = False

    def handle(self, request):
        assert request.headers["X-MemoryGate-Correction-Key"] == "k" * 32
        assert "X-MemoryGate-Key" not in request.headers
        assert request.headers["X-Agent-Id"] == "companion-memory"
        identity = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET" and "/memories/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": identity,
                    "agent_id": "companion-memory",
                    "revision": self.revision,
                    "text": self.text,
                    "source_type": "user",
                    "confidence": "high",
                },
            )
        if request.method == "GET":
            return httpx.Response(
                200 if identity in self.receipts else 404, json=self.receipts.get(identity, {})
            )
        body = json.loads(request.content)
        self.writes.append(body)
        if body["expected_revision"] != self.revision:
            return httpx.Response(409)
        self.text = body["text"]
        self.revision += 1
        receipt = {
            "request_id": identity,
            "memory_id": body["memory_id"],
            "agent_id": "companion-memory",
            "previous_revision": self.revision - 1,
            "revision": self.revision,
            "status": "applied",
        }
        self.receipts[identity] = receipt
        if self.lose_response:
            raise httpx.ReadTimeout("sensitive error text must never be persisted")
        return httpx.Response(200, json=receipt)


@pytest.fixture()
def setup(tmp_path):
    remote = Remote()
    with (
        closing(Store(tmp_path / "pi.db")) as store,
        closing(
            Client(
                "https://memory.test",
                "k" * 32,
                "companion-memory",
                transport=httpx.MockTransport(remote.handle),
            )
        ) as client,
    ):
        sid = store.create_session()
        message = store.append_message(sid, "user", "I prefer tea now.")
        body = p.Create(
            request_id="memory_proposal_001",
            session_id=sid,
            memory_id="mem_1",
            expected_memory_revision=3,
            text="Prefers tea",
            reason="Owner corrected this preference.",
            basis="stated",
            source_message_ids=[message["id"]],
        )
        yield store, client, remote, body


def test_proposal_review_is_separate_from_effect_and_parallel_approval_is_once(setup):
    store, client, remote, body = setup
    saved = p.create(store, body, client)
    assert saved["baseline"]["text"] == "Old preference" and saved["state"] == "pending"
    assert remote.writes == []
    with ThreadPoolExecutor(2) as pool:
        list(
            pool.map(
                lambda _: p.decide(store, saved["id"], p.Decision(decision="apply"), client),
                range(2),
            )
        )
    assert len(remote.writes) == 1 and remote.text == "Prefers tea"
    assert p.get(store, saved["id"])["state"] == "applied"
    assert p.create(store, body, None)["state"] == "applied"
    assert p.decide(store, saved["id"], p.Decision(decision="reject"), client)["state"] == "applied"


def test_lost_reply_reconciles_receipt_without_repeating_edit(setup):
    store, client, remote, body = setup
    remote.lose_response = True
    p.create(store, body, client)
    assert (
        p.decide(store, body.request_id, p.Decision(decision="apply"), client)["state"] == "unknown"
    )
    assert (
        p.decide(store, body.request_id, p.Decision(decision="apply"), client)["state"] == "unknown"
    )
    assert p.reconcile(store, body.request_id, client)["state"] == "applied"
    assert len(remote.writes) == 1
    assert "sensitive error" not in str(p.get(store, body.request_id))


def test_newer_memory_edit_conflicts_instead_of_overwriting(setup):
    store, client, remote, body = setup
    p.create(store, body, client)
    remote.revision, remote.text = 4, "Newer owner edit"
    assert (
        p.decide(store, body.request_id, p.Decision(decision="apply"), client)["state"]
        == "conflict"
    )
    assert remote.text == "Newer owner edit"


def test_rejection_and_private_sources_never_dispatch(setup):
    store, client, remote, body = setup
    p.create(store, body, client)
    assert (
        p.decide(store, body.request_id, p.Decision(decision="reject"), client)["state"]
        == "rejected"
    )
    p.decide(store, body.request_id, p.Decision(decision="apply"), client)
    session_settings.save(
        store,
        body.session_id,
        session_settings.Update(
            expected_revision=0,
            settings=session_settings.Settings(
                agentId="companion",
                privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=False),
            ),
        ),
    )
    with pytest.raises(agents.AgentError, match="long-term memory"):
        p.create(store, body.model_copy(update={"request_id": "memory_proposal_002"}), client)
    assert remote.writes == []


def test_restart_does_not_reapply_unknown_and_forgotten_proposal_is_scrubbed(tmp_path):
    path = tmp_path / "pi.db"
    remote = Remote()
    secret = "proposal-private-content-8812937"
    with closing(
        Client(
            "https://memory.test",
            "k" * 32,
            "companion-memory",
            transport=httpx.MockTransport(remote.handle),
        )
    ) as client:
        with closing(Store(path)) as store:
            sid = store.create_session()
            msg = store.append_message(sid, "user", secret)
            body = p.Create(
                request_id="interrupted_proposal_01",
                session_id=sid,
                memory_id="mem_1",
                expected_memory_revision=3,
                text=secret,
                reason=secret,
                basis="inferred",
                source_message_ids=[msg["id"]],
            )
            p.create(store, body, client)
            with store._connect() as db:
                db.execute(
                    "UPDATE memory_proposals SET state='applying' WHERE id=?", (body.request_id,)
                )
        with closing(Store(path)) as store:
            assert p.recover_interrupted(store) == 1
            p.decide(store, body.request_id, p.Decision(decision="apply"), client)
            assert p.reconcile(store, body.request_id, client)["state"] == "unknown"
            assert remote.writes == []
        forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])
        for file in tmp_path.iterdir():
            if file.is_file():
                assert secret.encode() not in file.read_bytes()


def test_wrong_namespace_and_oversize_response_rejected():
    for response in ({"agent_id": "foreign"}, {"agent_id": "a", "text": "x" * 100001}):
        with (
            closing(
                Client(
                    "https://memory.test",
                    "k" * 32,
                    "a",
                    transport=httpx.MockTransport(
                        lambda _, value=response: httpx.Response(200, json=value)
                    ),
                )
            ) as client,
            pytest.raises(ValueError),
        ):
            client.memory("mem_1")


def test_owner_routes_require_authorization_and_return_review(setup, monkeypatch):
    from fastapi.testclient import TestClient

    from pi import api

    store, client, remote, body = setup
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "owner-test-key-12345", raising=False)
    monkeypatch.setattr(api.app.state, "gateway_key_hash", "", raising=False)
    monkeypatch.setattr(api.app.state, "memory_corrections", client, raising=False)
    browser = TestClient(api.app)
    assert browser.post("/memory-proposals", json=body.model_dump()).status_code == 401
    headers = {"X-Pi-Key": "owner-test-key-12345"}
    response = browser.post("/memory-proposals", json=body.model_dump(), headers=headers)
    assert response.status_code == 200 and response.json()["state"] == "pending"
    assert remote.writes == []
    result = browser.post(
        "/memory-proposals/" + body.request_id + "/decision",
        json={"decision": "apply"},
        headers=headers,
    )
    assert result.status_code == 200 and result.json()["state"] == "applied"
