import hashlib
from contextlib import closing
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_setup_status import OWNER, Adapter, Memory, Router

from pi import api, setup_choices
from pi.browser_contract import owner_allowed, runtime_allowed
from pi.store import Store

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def value(request_id, choice, revision):
    return setup_choices.ChoiceInput(
        requestId=request_id, choice=choice, expectedRevision=revision
    )


def test_optional_choices_are_append_only_revisioned_and_restart_stable(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        assert setup_choices.current(store, "memory").model_dump(mode="json") == {
            "step": "memory",
            "revision": 0,
            "requestId": None,
            "choice": "undecided",
            "recordedAt": None,
        }
        skipped = setup_choices.record(
            store, "memory", value("setup-choice-memory-1", "skip", 0), now=NOW
        )
        replay = setup_choices.record(
            store, "memory", value("setup-choice-memory-1", "skip", 0), now=NOW
        )
        assert replay == skipped
        with pytest.raises(setup_choices.ChoiceError, match="different choice"):
            setup_choices.record(
                store, "memory", value("setup-choice-memory-1", "include", 1), now=NOW
            )
        with pytest.raises(setup_choices.ChoiceError, match="Expected revision 1"):
            setup_choices.record(
                store, "memory", value("setup-choice-memory-2", "include", 0), now=NOW
            )
        included = setup_choices.record(
            store, "memory", value("setup-choice-memory-2", "include", 1), now=NOW
        )
        assert included.revision == 2 and included.choice == "include"
        accepted = setup_choices.record(
            store,
            "companion",
            value("setup-choice-companion-1", "accept", 0),
            now=NOW,
        )
        assert accepted.revision == 1 and accepted.choice == "accept"
        with pytest.raises(setup_choices.ChoiceError, match="Companion accepts"):
            setup_choices.record(
                store,
                "companion",
                value("setup-choice-companion-invalid", "skip", 1),
                now=NOW,
            )
        with store._connect() as db, pytest.raises(Exception, match="append-only"):
            db.execute("UPDATE setup_optional_choices SET choice='skip'")

    with closing(Store(path)) as store:
        current = setup_choices.current(store, "memory")
        assert current.revision == 2 and current.choice == "include"
        assert setup_choices.current(store, "companion").choice == "accept"


def test_optional_choice_routes_are_exact_owner_operations(tmp_path, monkeypatch):
    store = Store(tmp_path / "pi.db")
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "memory", Memory(configured=False), raising=False)
    monkeypatch.setattr(api.app.state, "toolgate", None, raising=False)
    monkeypatch.setattr(api.app.state, "router", Router(Adapter()), raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "admin-" + "a" * 32, raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    monkeypatch.setattr(
        api.app.state,
        "gateway_key_hash",
        hashlib.sha256(("r" * 32).encode()).hexdigest(),
        raising=False,
    )
    client = TestClient(api.app)
    headers = {"X-Pi-Owner-Key": OWNER}
    try:
        assert client.get("/setup/choices/memory").status_code == 401
        current = client.get("/setup/choices/memory", headers=headers)
        assert current.status_code == 200 and current.json()["choice"] == "undecided"
        saved = client.post(
            "/setup/choices/memory",
            json={
                "requestId": "setup-choice-memory-api",
                "choice": "skip",
                "expectedRevision": 0,
            },
            headers=headers,
        )
        assert saved.status_code == 200
        assert saved.json()["revision"] == 1 and saved.json()["choice"] == "skip"
        assert client.get("/setup/choices/unknown", headers=headers).status_code == 403
        companion = client.post(
            "/setup/choices/companion",
            json={
                "requestId": "setup-choice-companion-api",
                "choice": "accept",
                "expectedRevision": 0,
            },
            headers=headers,
        )
        assert companion.status_code == 200 and companion.json()["choice"] == "accept"
    finally:
        store.close()

    for method in ("GET", "POST"):
        assert owner_allowed(method, "/setup/choices/companion")
        assert owner_allowed(method, "/setup/choices/memory")
        assert owner_allowed(method, "/setup/choices/capabilities")
        assert not owner_allowed(method, "/setup/choices/unknown")
        assert not runtime_allowed(method, "/setup/choices/memory")
