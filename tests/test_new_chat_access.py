"""The access a chat the owner starts begins with (`chats.default_access`).

"Full" lets Conker act without asking, so the choice lives in the owner preferences, where a
save through the gateway needs the password, and never in the owner settings document.
"""

import copy
import hashlib
import json
from contextlib import closing
from datetime import datetime
from typing import get_args

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore
from pi.api import server as api
from pi.api.browser_contract import owner_allowed, runtime_allowed, session_only_write
from pi.conversation import drafts
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.owner import preferences as prefs
from pi.schedule import agent_jobs, job_models
from pi.turns import access

ORIGIN = "https://localhost:8050"
PASSWORD = "new chat access testing phrase"
OWNER = "new-chat-access-owner-key-" + "o" * 32
ADMIN = "new-chat-access-admin-key"
PATH = "/api/control/pi/owner/preferences"
NOON = datetime.fromisoformat("2026-10-26T10:00:00+00:00").timestamp()


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "chats.db")) as value:
        yield value


def preferences(**changes):
    return {**copy.deepcopy(prefs.DEFAULT), **changes}


def choose(store, level):
    revision = prefs.load(store)["revision"]
    request = prefs.UpdatePreferences(
        expected_revision=revision,
        preferences=prefs.OwnerPreferences.model_validate(preferences(newChatAccess=level)),
    )
    return prefs.save(store, request)


def access_of(store, session_id):
    return session_settings.load(store, session_id)["settings"].get("access", "default")


def test_the_choices_are_exactly_the_chat_access_switch():
    assert prefs.CHAT_ACCESS == access.CHAT_LEVELS
    field = prefs.OwnerPreferences.model_fields["newChatAccess"].annotation
    assert get_args(field) == prefs.CHAT_ACCESS
    assert get_args(session_settings.Settings.model_fields["access"].annotation) == (
        prefs.CHAT_ACCESS
    )
    for level in prefs.CHAT_ACCESS:
        assert prefs.OwnerPreferences.model_validate(preferences(newChatAccess=level))
    for value in ("allow", "agent", "FULL", "", True, None, ["full"]):
        with pytest.raises(ValidationError):
            prefs.OwnerPreferences.model_validate(preferences(newChatAccess=value))
    older = copy.deepcopy(prefs.DEFAULT)
    del older["newChatAccess"]
    assert prefs.OwnerPreferences.model_validate(older).newChatAccess == "default"


def test_the_reader_returns_the_saved_choice_and_default_for_anything_else(store):
    with store._connect() as db:
        assert prefs.new_chat_access(db) == "default"
    assert choose(store, "full")["preferences"]["newChatAccess"] == "full"
    with store._connect() as db:
        assert prefs.new_chat_access(db) == "full"
    for raw in (
        '{"newChatAccess": "allow"}',
        '{"newChatAccess": true}',
        '{"newChatAccess": ["full"]}',
        "[]",
        "not json",
        "{}",
    ):
        with store._connect() as db:
            db.execute("UPDATE owner_preferences SET preferences=? WHERE singleton=1", (raw,))
            db.commit()
            assert prefs.new_chat_access(db) == "default"
    with store._connect() as db:
        db.execute("DELETE FROM owner_preferences")
        db.commit()
        assert prefs.new_chat_access(db) == "default"


def test_an_older_document_reads_the_default(store):
    older = copy.deepcopy(prefs.DEFAULT)
    del older["newChatAccess"]
    with store._connect() as db:
        db.execute(
            "UPDATE owner_preferences SET preferences=? WHERE singleton=1",
            (json.dumps(older),),
        )
        db.commit()
    assert prefs.load(store)["preferences"]["newChatAccess"] == "default"
    with store._connect() as db:
        assert prefs.new_chat_access(db) == "default"


def test_saving_it_needs_the_password_at_the_gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"revision": 2, "preferences": preferences()})

    config = Config(ORIGIN, str(auth.path), "http://pi:8050", "r" * 32, pi_owner_key=OWNER)
    app = create_app(config, store=auth, transport=httpx.MockTransport(upstream))
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
        body = {"expected_revision": 1, "preferences": preferences(newChatAccess="full")}
        refused = client.post(PATH, json=body, headers=headers)
        assert refused.status_code == 428
        assert refused.json()["detail"]["code"] == "verification_required"

        def proof(operation):
            answer = client.post(
                "/auth/verify",
                headers=headers,
                json={
                    "password": PASSWORD,
                    "operation": {"method": "POST", "path": PATH, "body": operation},
                },
            )
            assert answer.status_code == 200, answer.text
            return {**headers, "X-Conker-Verification": answer.json()["verification_token"]}

        # A password given for a milder choice does not cover Full.
        milder = proof({**body, "preferences": preferences(newChatAccess="ask")})
        assert client.post(PATH, json=body, headers=milder).status_code == 428
        # The chat's runtime channel has no way to it at all.
        assert client.post("/api/pi/owner/preferences", json=body, headers=headers).status_code == (
            403
        )
        assert not seen
        assert client.post(PATH, json=body, headers=proof(body)).status_code == 200
    assert len(seen) == 1 and seen[0].url.path == "/owner/preferences"
    assert seen[0].headers["x-pi-owner-key"] == OWNER
    assert owner_allowed("POST", "/owner/preferences")
    assert not session_only_write("POST", "/owner/preferences")
    assert not runtime_allowed("POST", "/owner/preferences")


def test_pi_saves_it_only_with_the_owner_credential(store, monkeypatch):
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", ADMIN, raising=False)
    monkeypatch.setattr(
        api.app.state, "gateway_key_hash", hashlib.sha256(b"runtime_key").hexdigest(), raising=False
    )
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    client = TestClient(api.app)
    body = {"expected_revision": 1, "preferences": preferences(newChatAccess="full")}
    for headers in ({}, {"X-Pi-Gateway-Key": "runtime_key"}):
        assert client.post("/owner/preferences", json=body, headers=headers).status_code == 401
    assert prefs.load(store)["preferences"]["newChatAccess"] == "default"
    saved = client.post("/owner/preferences", json=body, headers={"X-Pi-Owner-Key": OWNER})
    assert saved.status_code == 200 and saved.json()["preferences"]["newChatAccess"] == "full"


def test_chats_that_exist_and_ai_job_chats_keep_their_own_access(store):
    before = store.create_session(title="Before")
    asked = store.create_session(title="Asked")
    current = session_settings.load(store, asked)
    session_settings.save(
        store,
        asked,
        session_settings.Update(
            expected_revision=current["revision"],
            settings={**current["settings"], "access": "ask"},
        ),
    )
    choose(store, "full")
    assert access_of(store, before) == "default"
    assert access_of(store, asked) == "ask"
    # A cron job's chat runs on his instructions with its own access, never the new-chat default.
    job = agent_jobs.create(
        store,
        job_models.AgentJobCreate.model_validate(
            dict(
                name="Watch my week",
                instructions="Look for exams and deadlines.",
                timing=dict(kind="interval", time="00:00", day=0, hours=2),
                timeZone="Asia/Jerusalem",
            )
        ),
        now=NOON,
    )
    assert access_of(store, agent_jobs.session_for(store, job["id"])) == "default"


def test_a_new_chat_begins_with_the_saved_access(store, monkeypatch):
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", ADMIN, raising=False)
    choose(store, "full")
    created = TestClient(api.app).post(
        "/sessions", json={"title": "New"}, headers={"X-Pi-Key": ADMIN}
    )
    assert created.status_code == 200
    assert access_of(store, created.json()["session_id"]) == "full"


def test_a_draft_started_as_a_chat_begins_with_the_saved_access(store):
    choose(store, "ai")
    drafts.save_new(store, drafts.NewSave(expected_revision=0, text="Plan my week"))
    started = drafts.start(
        store, drafts.Start(request_id="new_chat_access_01", expected_revision=1)
    )
    assert access_of(store, started["session_id"]) == "ai"
