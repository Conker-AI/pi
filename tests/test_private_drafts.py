"""Private composer state survives reload without becoming a message or permission."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_background_thinking import setup
from test_loop import Recorder, loop_with

from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore
from pi.agents import profiles as agents
from pi.agents import skills
from pi.api import server as api
from pi.conversation import drafts, forgetting, submissions
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.schedule import tasks
from pi.setup import choices as setup_choices
from pi.turns.failures import TurnFailed


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "drafts.db")) as value:
        yield value


def intent(**changes):
    return {
        "text": "Unsent private request",
        "model_id": "a",
        "skill_ids": ["review"],
        "think": True,
        "research_mode": "web",
        **changes,
    }


def test_options_roundtrip_legacy_read_and_strict_bounded_write(store):
    sid = store.create_session()
    saved = drafts.save(store, sid, drafts.Save(expected_revision=0, **intent()))
    assert saved["revision"] == 1
    assert all(saved[key] == value for key, value in intent().items())
    assert saved == drafts.load(store, sid)
    assert store.messages(sid) == []
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM turn_submissions").fetchone()[0] == 0
        db.execute("UPDATE conversation_drafts SET text=? WHERE session_id=?", ("x" * 100000, sid))
    assert len(drafts.load(store, sid)["text"]) == 100000
    with pytest.raises(ValidationError):
        drafts.Save(expected_revision=1, text="x" * 16001)
    with pytest.raises(drafts.DraftError, match="size limit"):
        drafts.save(store, sid, drafts.Save(expected_revision=1, text="\x00" * 16000))
    for field in ({"think": "true"}, {"expected_revision": True}, {"unknown": 1}, {"model_id": ""}):
        with pytest.raises(ValidationError):
            drafts.Save.model_validate({"expected_revision": 1, "text": "hi", **field})
    with pytest.raises((ValidationError, agents.AgentError)):
        drafts.Save(expected_revision=1, text="hi", skill_ids=["duplicate", "duplicate"])


def test_load_is_one_coherent_transaction(store, monkeypatch):
    sid = store.create_session()
    drafts.save(store, sid, drafts.Save(expected_revision=0, text="one", research_mode="web"))
    scope = drafts._scope

    def concurrent_save(db, session_id, task_id):
        result = scope(db, session_id, task_id)
        assert db.in_transaction
        drafts.save(store, sid, drafts.Save(expected_revision=1, text="two", research_mode="deep"))
        return result

    monkeypatch.setattr(drafts, "_scope", concurrent_save)
    # The writer must not recurse through the intercepted read path.
    original_save = drafts.save

    def write(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(drafts, "_scope", scope)
            return original_save(*args, **kwargs)

    monkeypatch.setattr(drafts, "save", write)
    value = drafts.load(store, sid)
    assert (value["revision"], value["text"], value["research_mode"]) == (1, "one", "web")


def test_start_is_atomic_idempotent_and_preserves_saved_privacy(store):
    value = drafts.NewSave(
        expected_revision=0,
        title="Private chat",
        agent_id="companion",
        privacy={"memoryDisabled": True, "harnessDisabled": True},
        **intent(),
    )
    drafts.save_new(store, value)
    request = drafts.Start(request_id="new_chat_request_01", expected_revision=1)
    first = drafts.start(store, request)
    sid = first["session_id"]
    assert first["draft_revision"] == 1 and first["source_revision"] == 1
    saved = drafts.load(store, sid)
    assert all(saved[key] == value for key, value in intent().items())
    assert store.get_session(sid)["title"] == "Private chat"
    settings = session_settings.load(store, sid)["settings"]
    assert settings["privacy"] == {"memoryDisabled": True, "harnessDisabled": True}
    assert settings["agentId"] == "companion" and store.messages(sid) == []
    cleared = drafts.load_new(store)
    assert cleared["revision"] == 2 and cleared["text"] == "" and not cleared["think"]
    drafts.save_new(store, drafts.NewSave(expected_revision=2, text="Next idea"))
    assert drafts.start(store, request) == first
    assert drafts.get_start(store, request.request_id) == first
    assert drafts.load_new(store)["text"] == "Next idea"
    assert len(store.list_sessions()) == 1
    with pytest.raises(drafts.DraftError, match="already used"):
        drafts.start(store, request.model_copy(update={"expected_revision": 3}))


def test_concurrent_start_creates_exactly_one_session(store):
    drafts.save_new(store, drafts.NewSave(expected_revision=0, text="hello"))

    def start(index):
        try:
            return drafts.start(
                store, drafts.Start(request_id=f"concurrent_start_{index}", expected_revision=1)
            )
        except drafts.DraftError:
            return None

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(start, range(2)))
    assert len([value for value in outcomes if value]) == 1
    assert len(store.list_sessions()) == 1


def test_new_draft_preserves_inherited_memory_default_and_explicit_override(store):
    setup_choices.record(
        store,
        "memory",
        setup_choices.ChoiceInput(
            requestId="draft_memory_default_skip", choice="skip", expectedRevision=0
        ),
    )
    drafts.save_new(store, drafts.NewSave(expected_revision=0, text="Inherit my privacy"))
    inherited = drafts.start(
        store, drafts.Start(request_id="inherit_private_default", expected_revision=1)
    )
    assert (
        session_settings.load(store, inherited["session_id"])["settings"]["privacy"][
            "memoryDisabled"
        ]
        is True
    )
    drafts.save_new(
        store,
        drafts.NewSave(
            expected_revision=2, text="Explicit owner override", privacy={"memoryDisabled": False}
        ),
    )
    assert drafts.load_new(store)["privacy"] == {"memoryDisabled": False}
    explicit = drafts.start(
        store, drafts.Start(request_id="explicit_memory_override", expected_revision=3)
    )
    assert (
        session_settings.load(store, explicit["session_id"])["settings"]["privacy"][
            "memoryDisabled"
        ]
        is False
    )


def test_failed_promotion_rolls_back_all_changes_and_can_retry(store, monkeypatch):
    drafts.save_new(store, drafts.NewSave(expected_revision=0, text="Keep me"))
    request = drafts.Start(request_id="rollback_start_request", expected_revision=1)
    original = drafts._write_new

    def fail(db, body):
        raise RuntimeError("storage unavailable before commit")

    monkeypatch.setattr(drafts, "_write_new", fail)
    with pytest.raises(RuntimeError):
        drafts.start(store, request)
    assert store.list_sessions() == [] and drafts.load_new(store)["revision"] == 1
    monkeypatch.setattr(drafts, "_write_new", original)
    assert drafts.start(store, request)["draft_revision"] == 1


def test_unavailable_agent_preserves_unbound_draft(store):
    drafts.save_new(
        store, drafts.NewSave(expected_revision=0, text="Keep me", agent_id="agent_" + "a" * 32)
    )
    with pytest.raises(agents.AgentError):
        drafts.start(store, drafts.Start(request_id="missing_agent_start", expected_revision=1))
    assert not store.list_sessions() and drafts.load_new(store)["revision"] == 1


def test_full_intent_reservation_freezes_revision_and_newer_edits_survive(store):
    loop, _provider = setup(store)
    sid = store.create_session()
    skills.import_skill(
        store,
        skills.Import(
            expected_revision=0,
            markdown="---\nname: review\ndescription: Review\n---\nCheck this carefully.",
        ),
    )
    saved = intent(research_mode="off")
    drafts.save(store, sid, drafts.Save(expected_revision=0, **saved))
    for change in ({"model_id": None}, {"skill_ids": []}, {"think": False}):
        with pytest.raises(submissions.SubmissionError, match="saved draft"):
            altered = {**saved, **change}
            text = altered.pop("text")
            submissions.reserve(
                store, "draft_options_request", sid, text, {}, draft_revision=1, **altered
            )
    text = saved.pop("text")
    first, created = submissions.reserve(
        store, "draft_options_request", sid, text, {}, draft_revision=1, **saved
    )
    assert created and first["draft_revision"] == 1
    drafts.save(store, sid, drafts.Save(expected_revision=1, text="Next", model_id="b"))
    submissions.bind(store, "draft_options_request")
    replay, created = submissions.reserve(
        store, "draft_options_request", sid, text, {}, draft_revision=1, **saved
    )
    assert not created and replay["draft_revision"] == 1
    assert drafts.load(store, sid)["model_id"] == "b"
    with pytest.raises(submissions.SubmissionError, match="already used"):
        submissions.reserve(
            store, "draft_options_request", sid, text, {}, draft_revision=2, **saved
        )
    loop.thinking.close()


def test_preparation_failure_retains_exact_draft(store):
    sid = store.create_session()
    saved = drafts.save(store, sid, drafts.Save(expected_revision=0, text="Keep me"))
    submissions.reserve(store, "draft_failed_request", sid, "Keep me", {}, draft_revision=1)
    submissions.fail_preparation(store, "draft_failed_request")
    assert drafts.load(store, sid) == saved
    assert submissions.get(store, "draft_failed_request")["draft_revision"] == 1
    assert store.messages(sid) == []


def test_bound_input_consumes_draft_even_if_inference_fails(store):
    sid = store.create_session()
    drafts.save(store, sid, drafts.Save(expected_revision=0, text="Durable message"))
    with pytest.raises(TurnFailed):
        loop_with(store, Recorder(fail=True)).run_turn(
            sid, "Durable message", request_id="failed_model_draft_request", draft_revision=1
        )
    assert drafts.load(store, sid)["text"] == ""
    assert store.messages(sid)[0]["content"] == "Durable message"
    assert submissions.get(store, "failed_model_draft_request")["draft_revision"] == 1


def test_fork_consumes_original_draft_and_receipt_keeps_revision(store):
    sid = store.create_session()
    store.append_message(sid, "user", "x" * 120)
    drafts.save(store, sid, drafts.Save(expected_revision=0, text="Continue"))
    result = loop_with(store, Recorder("summary"), fork_threshold_chars=100).run_turn(
        sid, "Continue", request_id="forked_draft_request", draft_revision=1
    )
    assert result["session_id"] != sid
    assert result["submission"]["draft_revision"] == 1
    assert drafts.load(store, sid)["text"] == ""


def test_forgetting_scrubs_promoted_source_options_and_fences_replay(tmp_path):
    path = tmp_path / "forget.db"
    secret = "unique-private-composer-621739"
    request = drafts.Start(request_id="forget_start_request", expected_revision=1)
    with closing(Store(path)) as store:
        drafts.save_new(
            store, drafts.NewSave(expected_revision=0, text=secret, model_id=secret, title=secret)
        )
        sid = drafts.start(store, request)["session_id"]
    forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])
    with closing(Store(path)) as store:
        replay = drafts.start(store, request)
        assert replay["content_status"] == "forgotten" and replay["draft_revision"] is None
        assert drafts.get_start(store, request.request_id) == replay
        with pytest.raises(drafts.DraftError, match="unavailable"):
            drafts.save(store, sid, drafts.Save(expected_revision=1, text=secret))
        assert drafts.load_new(store)["text"] == ""
        assert len(store.list_sessions()) == 1
    for file in tmp_path.glob("forget.db*"):
        assert secret.encode() not in file.read_bytes()


def test_api_owner_scope_validation_and_legacy_recovery(store, monkeypatch):
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "recovery", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(b"owner").hexdigest(), raising=False
    )
    monkeypatch.setattr(
        api.app.state, "gateway_key_hash", hashlib.sha256(b"runtime").hexdigest(), raising=False
    )
    client = TestClient(api.app)
    owner, recovery = {"X-Pi-Owner-Key": "owner"}, {"X-Pi-Key": "recovery"}
    sid = store.create_session()
    path = f"/drafts/{sid}"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Pi-Gateway-Key": "runtime"}).status_code == 401
    assert client.get(path, headers=owner).json()["revision"] == 0
    assert (
        client.put(path, json={"expected_revision": 0, "text": "legacy"}, headers=owner).status_code
        == 403
    )
    assert (
        client.put(
            path, json={"expected_revision": 0, "text": "legacy"}, headers=recovery
        ).status_code
        == 200
    )
    assert (
        client.post(path, json={"expected_revision": 1, "text": "next"}, headers=owner).status_code
        == 200
    )
    assert (
        client.post(path, json={"expected_revision": 1, "text": "stale"}, headers=owner).json()[
            "detail"
        ]["code"]
        == "draft_conflict"
    )
    for suffix in ("?unexpected=1", "?task_id=a&task_id=b"):
        assert client.get(path + suffix, headers=owner).status_code == 422
    assert (
        client.post(
            path + "?task_id=a", json={"expected_revision": 2, "text": "bad"}, headers=owner
        ).status_code
        == 422
    )
    assert (
        client.post(
            path, json={"expected_revision": 2, "text": "bad", "grant": True}, headers=owner
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/drafts/new",
            json={"expected_revision": 0, "text": "new", "privacy": {"memoryDisabled": True}},
            headers=owner,
        ).status_code
        == 200
    )
    started = client.post(
        "/drafts/new/start",
        json={"expected_revision": 1, "request_id": "api_new_draft_start"},
        headers=owner,
    )
    assert started.status_code == 200 and started.json()["draft_revision"] == 1
    lookup = "/drafts/new/start/api_new_draft_start"
    assert client.get(lookup, headers=owner).json() == started.json()
    assert client.get(lookup, headers={"X-Pi-Gateway-Key": "runtime"}).status_code == 401
    assert client.get(lookup + "?unexpected=1", headers=owner).status_code == 422
    missing = client.get("/drafts/new/start/no_receipt_exists", headers=owner)
    assert (
        missing.status_code == 404 and missing.json()["detail"]["code"] == "draft_start_not_found"
    )
    assert (
        client.post(
            path,
            content='{"expected_revision":2,"text":"one","text":"two"}',
            headers={**owner, "Content-Type": "application/json"},
        ).status_code
        == 422
    )
    oversized = '{"expected_revision":2,"text":"' + "\\u0061" * 16000 + '"}'
    assert (
        client.post(
            path, content=oversized, headers={**owner, "Content-Type": "application/json"}
        ).status_code
        == 413
    )


def test_task_draft_browser_alias_keeps_legacy_query_scope(store, monkeypatch):
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "recovery", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(b"owner").hexdigest(), raising=False
    )
    client = TestClient(api.app)
    sid = store.create_session()
    task = tasks.create(
        store,
        tasks.CreateTask(
            request_id="private_draft_task", session_id=sid, outcome="Report", criteria=["Written"]
        ),
    )
    owner, recovery = {"X-Pi-Owner-Key": "owner"}, {"X-Pi-Key": "recovery"}
    path = f"/drafts/{sid}/tasks/{task['id']}"
    result = client.post(path, json={"expected_revision": 0, "text": "Task only"}, headers=owner)
    assert result.status_code == 200 and result.json()["task_id"] == task["id"]
    assert (
        client.get(f"/drafts/{sid}?task_id={task['id']}", headers=recovery).json() == result.json()
    )
    assert client.get(f"/drafts/{sid}", headers=owner).json()["text"] == ""
    other = store.create_session()
    assert client.get(f"/drafts/{other}/tasks/{task['id']}", headers=owner).status_code == 404


def test_gateway_draft_autosave_requires_session_csrf_not_password(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password("local integration testing phrase")
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"revision": 1}, headers={"Set-Cookie": "upstream=bad"})

    origin = "https://localhost:8050"
    config = Config(origin, str(auth.path), "http://pi:8050", "r" * 32, pi_owner_key="o" * 32)
    with TestClient(
        create_app(config, store=auth, transport=httpx.MockTransport(upstream)), base_url=origin
    ) as client:
        path = "/api/control/pi/drafts/new"
        assert client.get(path).status_code == 401
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post(
            "/auth/login",
            json={"password": "local integration testing phrase"},
            headers={"Origin": origin, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}
        body = {"expected_revision": 0, "text": "private"}
        assert client.post(path, json=body).status_code == 403
        assert client.post(path + "?unexpected=1", json=body, headers=headers).status_code == 422
        response = client.post(path, json=body, headers={**headers, "X-Pi-Owner-Key": "attacker"})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
        assert seen[-1].headers["x-pi-owner-key"] == "o" * 32
        assert "cookie" not in seen[-1].headers and "x-pi-gateway-key" not in seen[-1].headers
        assert json.loads(seen[-1].content) == body
        assert (
            client.post(
                path + "/start",
                json={"request_id": "browser_start_request", "expected_revision": 1},
                headers=headers,
            ).status_code
            == 200
        )
        assert client.get("/api/pi/drafts/new").status_code == 403
