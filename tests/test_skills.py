"""Imported skill instructions remain explicit, immutable per turn and powerless."""

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_loop import Recorder, loop_with

from pi import agents, api, forgetting, session_settings, skills, submissions, turn_context
from pi.store import Store
from pi.toolgate import Tool


def document(name="review", body="Report uncertainty and cite the available evidence."):
    return f"---\nname: {name}\ndescription: Review supplied evidence\n---\n\n{body}\n"


def imported(store, name="review", body="Report uncertainty and cite the available evidence."):
    return skills.import_skill(
        store, skills.Import(markdown=document(name, body), expected_revision=0)
    )


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "skills.db")) as value:
        yield value


def test_import_update_state_and_exact_text_persist(store):
    markdown = document(body="First body.\r\nKeep these line endings.")
    first = skills.import_skill(store, skills.Import(markdown=markdown, expected_revision=0))
    assert first["id"] == "review" and first["enabled"] is True
    assert first["revision"] == 1 and first["markdown"] == markdown
    assert first["sha256"] == hashlib.sha256(markdown.encode()).hexdigest()
    assert "markdown" not in skills.list_skills(store)["results"][0]
    off = skills.set_state(store, "review", skills.State(expected_revision=1, enabled=False))
    assert off["revision"] == 2 and not off["enabled"]
    updated = skills.import_skill(store, skills.Import(markdown=document(), expected_revision=2))
    assert updated["revision"] == 3 and not updated["enabled"]
    assert updated["createdAt"] == first["createdAt"]
    with pytest.raises(agents.AgentError) as error:
        skills.import_skill(store, skills.Import(markdown=document(), expected_revision=2))
    assert error.value.detail["current_revision"] == 3
    assert skills.get(store, "review") == updated


@pytest.mark.parametrize(
    "markdown",
    [
        "No frontmatter",
        "---\nname: review\n---\nBody",
        document("Uppercase"),
        document("two--hyphens"),
        "---\nname: review\ndescription: hello\n---\n",
        "---\nname: review\nname: other\ndescription: duplicate\n---\nBody",
        "---\nname: review\ndescription: &alias hello\nmetadata: *alias\n---\nBody",
        "---\nname: review\ndescription: !!python/object/apply:os.system [echo bad]\n---\nBody",
        document(body="\x00"),
        document("x" * 65),
        document(body="x" * 32000),
    ],
)
def test_bad_frontmatter_rejected_without_execution(store, markdown):
    with pytest.raises((agents.AgentError, ValueError)):
        skills.import_skill(store, skills.Import(markdown=markdown, expected_revision=0))
    assert skills.list_skills(store)["results"] == []


def test_standard_folded_frontmatter_and_inert_metadata(store):
    markdown = "---\nname: review\ndescription: >\n  Review the\n  supplied evidence.\nmetadata:\n  author: Owner\nallowed-tools: shell\n---\nUse references/example.md when available."
    value = skills.import_skill(store, skills.Import(markdown=markdown, expected_revision=0))
    assert value["description"] == "Review the supplied evidence."
    assert "allowed-tools" not in value


def test_registry_and_combined_limits(store):
    for number in range(100):
        imported(store, f"skill-{number}", "x" * (22000 if number < 3 else 1))
    with pytest.raises(agents.AgentError, match="100"):
        imported(store, "one-more")
    sid = store.create_session()
    with pytest.raises(agents.AgentError, match="64000"):
        submissions.reserve(
            store,
            "oversized_skills_request",
            sid,
            "Review",
            {},
            skill_ids=["skill-0", "skill-1", "skill-2"],
        )
    assert store.messages(sid) == []


@pytest.mark.parametrize(
    "selected", [["unknown"], ["review", "review"], ["bad/name"], ["review"] * 4]
)
def test_invalid_selection_never_reaches_model(store, selected):
    imported(store)
    provider = Recorder()
    loop = loop_with(store, provider)
    sid = store.create_session()
    with pytest.raises(agents.AgentError):
        loop.run_turn(sid, "Review", request_id="invalid_skill_request", skill_ids=selected)
    assert provider.calls == [] and store.messages(sid) == []


def test_disabled_and_no_harness_rejected_before_reservation(store):
    imported(store)
    skills.set_state(store, "review", skills.State(expected_revision=1, enabled=False))
    sid = store.create_session()
    with pytest.raises(agents.AgentError, match="Enable"):
        submissions.reserve(
            store, "disabled_skill_request", sid, "Review", {}, skill_ids=["review"]
        )
    skills.set_state(store, "review", skills.State(expected_revision=2, enabled=True))
    session_settings.save(
        store,
        sid,
        session_settings.Update(
            expected_revision=0,
            settings=session_settings.Settings(
                agentId="companion",
                privacy=session_settings.Privacy(memoryDisabled=False, harnessDisabled=True),
            ),
        ),
    )
    with pytest.raises(agents.AgentError, match="No-harness"):
        submissions.reserve(
            store, "no_harness_skill_request", sid, "Review", {}, skill_ids=["review"]
        )
    assert submissions.list_pending(store, sid)["results"] == []


def test_frozen_selection_injection_replay_and_request_identity(store, monkeypatch):
    original = imported(store, body="Use the original checklist.")
    provider = Recorder()
    loop = loop_with(store, provider)

    class Gate:
        def tools(self):
            return [Tool("read", "Read", "Existing permitted read", [])]

    loop.toolgate = Gate()
    available = [tool.id for tool in loop._available_tools()]
    sid = store.create_session()
    original_run = loop._run_bound

    def change_before_answer(*args, **kwargs):
        skills.import_skill(
            store,
            skills.Import(markdown=document(body="Different checklist."), expected_revision=1),
        )
        skills.set_state(store, "review", skills.State(expected_revision=2, enabled=False))
        return original_run(*args, **kwargs)

    monkeypatch.setattr(loop, "_run_bound", change_before_answer)
    result = loop.run_turn(sid, "Review", request_id="frozen_skill_request", skill_ids=["review"])
    execution = session_settings.execution(store, sid, turn_id=result["turn_id"])
    assert execution["skills"] == [original]
    supplied = [message.content for message in provider.calls[0]]
    assert any(original["markdown"] in text and "does not grant tools" in text for text in supplied)
    assert not any("Different checklist" in text for text in supplied)
    assert [tool.id for tool in loop._available_tools()] == available
    assert execution["authority"] == "none"
    assert turn_context.replay(store, result["turn_id"])["messages"] == provider.calls[0]
    assert submissions.get(store, "frozen_skill_request")["skill_ids"] == ["review"]
    repeated = loop.run_turn(sid, "Review", request_id="frozen_skill_request", skill_ids=["review"])
    assert repeated["replayed"] is True and len(provider.calls) == 1
    with pytest.raises(submissions.SubmissionError, match="already used"):
        loop.run_turn(sid, "Review", request_id="frozen_skill_request", skill_ids=[])
    assert not any(original["markdown"] in message.content for message in loop._history(sid))


def test_auth_api_and_turn_selection(store, monkeypatch):
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "skills_admin_key", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(b"owner_key").hexdigest(), raising=False
    )
    monkeypatch.setattr(
        api.app.state, "gateway_key_hash", hashlib.sha256(b"runtime_key").hexdigest(), raising=False
    )
    provider = Recorder()
    monkeypatch.setattr(api.app.state, "loop", loop_with(store, provider), raising=False)
    client = TestClient(api.app)
    owner, runtime = {"X-Pi-Owner-Key": "owner_key"}, {"X-Pi-Gateway-Key": "runtime_key"}
    payload = {"markdown": document(), "expected_revision": 0}
    assert client.get("/skills").status_code == 401
    assert client.post("/skills/import", json=payload, headers=runtime).status_code == 401
    assert client.post("/skills/import", json=payload, headers=owner).status_code == 200
    assert client.get("/skills", headers=runtime).json()["results"][0]["id"] == "review"
    assert client.get("/skills/review", headers=runtime).json()["markdown"] == document()
    assert client.get("/skills/review", headers=owner).status_code == 200
    assert client.post("/skills/import", json=payload, headers=owner).status_code == 409
    assert (
        client.post(
            "/skills/review/state", json={"expected_revision": 1, "enabled": False}, headers=runtime
        ).status_code
        == 401
    )
    sid = store.create_session()
    response = client.post(
        f"/sessions/{sid}/turns",
        headers=runtime,
        json={"text": "Review", "request_id": "api_skill_request_1", "skill_ids": ["review"]},
    )
    assert response.status_code == 200, response.text
    assert any(document() in message.content for message in provider.calls[0])


def test_forgetting_removes_frozen_skill_copies_and_retains_registry(tmp_path):
    path = tmp_path / "forget.db"
    with closing(Store(path)) as store:
        imported(store, body="private-frozen-skill-content")
        sid = store.create_session()
        loop_with(store, Recorder()).run_turn(
            sid, "Review", request_id="forgotten_skill_request", skill_ids=["review"]
        )
        skills.import_skill(
            store,
            skills.Import(
                markdown=document(body="Current reusable instructions."), expected_revision=1
            ),
        )
    forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])
    with closing(Store(path)) as store:
        assert skills.get(store, "review")["revision"] == 2
        receipt = submissions.get(store, "forgotten_skill_request")
        assert receipt["skill_ids"] == [] and receipt["model_id"] is None
        with store._connect() as db:
            for table in ("submission_settings", "turn_settings"):
                assert all(
                    "private-frozen-skill-content" not in row[0]
                    for row in db.execute(f"SELECT snapshot FROM {table}")
                )
    for file in tmp_path.glob("forget.db*"):
        assert b"private-frozen-skill-content" not in file.read_bytes()
