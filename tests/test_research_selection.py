"""Research intent survives transport/queue replay without claiming execution."""

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_loop import Recorder, loop_with

from pi import api, drafts, research, session_settings, submissions, tasks
from pi import turn_queue as q
from pi.store import Store


@pytest.mark.parametrize("mode", ["web", "deep"])
def test_mode_frozen_in_submission_and_turn_after_reopen(tmp_path, mode):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        receipt, created = submissions.reserve(
            store, "research_request_001", sid, "Find sources", {}, research_mode=mode
        )
        assert created and receipt["research_mode"] == mode
        assert not submissions.reserve(
            store, "research_request_001", sid, "Find sources", {}, research_mode=mode
        )[1]
        with pytest.raises(submissions.SubmissionError):
            submissions.reserve(store, "research_request_001", sid, "Find sources", {})
        bound = submissions.bind(store, "research_request_001")
        tid = bound["turn_id"]
    with closing(Store(path)) as store:
        assert session_settings.execution(store, sid, turn_id=tid)["researchMode"] == mode
        assert submissions.get(store, "research_request_001")["research_mode"] == mode


def test_unavailable_runtime_retains_intent_without_dispatch_or_silent_fallback(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        provider = Recorder()
        loop = loop_with(store, provider)
        with pytest.raises(tasks.TaskError) as failure:
            loop.run_turn(
                sid, "Investigate", request_id="research_failure_001", research_mode="deep"
            )
        assert failure.value.detail["code"] == "research_unavailable"
        receipt = submissions.get(store, "research_failure_001")
        assert receipt["failure_code"] == "research_unavailable"
        assert receipt["research_mode"] == "deep" and receipt["turn_id"] is None
        assert provider.calls == [] and store.messages(sid) == []
        replay = loop.run_turn(
            sid, "Investigate", request_id="research_failure_001", research_mode="deep"
        )
        assert replay["replayed"] and provider.calls == []


def test_queue_preserves_mode_on_edit_review_and_requires_exact_admission(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        entry = q.enqueue(
            store,
            sid,
            q.Enqueue(request_id="research_queue_001", text="Find sources", research_mode="web"),
        )
        entry = q.change(
            store, sid, entry["id"], q.Edit(expected_revision=1, text="Find more sources"), "edit"
        )
        entry = q.change(store, sid, entry["id"], q.Review(expected_revision=2), "review")
        assert entry["payload"]["research_mode"] == "web"
        rid = q.submission_identity(entry["id"], entry["revision"])
        with pytest.raises(tasks.TaskError):
            submissions.reserve(
                store,
                rid,
                sid,
                "Find more sources",
                {},
                queued_entry=(entry["id"], entry["revision"]),
            )
        assert q.read(store, sid)["entries"][0]["state"] == "waiting"
        receipt, _ = submissions.reserve(
            store,
            rid,
            sid,
            "Find more sources",
            {},
            queued_entry=(entry["id"], entry["revision"]),
            research_mode="web",
        )
        assert receipt["research_mode"] == "web"


def test_off_is_legacy_compatible_and_changes_no_permissions(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        first, _ = submissions.reserve(store, "ordinary_request_001", sid, "Hello", {})
        replay, created = submissions.reserve(
            store, "ordinary_request_001", sid, "Hello", {}, research_mode="off"
        )
        assert not created and replay == first
        selected = session_settings.execution(store, sid, request_id="ordinary_request_001")
        assert "researchMode" not in selected
        research.require_runtime(selected)


@pytest.mark.parametrize("mode", ["auto", "", None, True, 1])
def test_invalid_modes_fail_at_api_and_internal_boundary(mode):
    with pytest.raises(ValidationError):
        api.TurnRequest(text="Find", research_mode=mode)
    with pytest.raises(ValidationError):
        q.Enqueue(request_id="research_queue_001", text="Find", research_mode=mode)
    with pytest.raises(tasks.TaskError):
        research.validate(mode)


def test_call_and_team_modes_do_not_gain_research_authority():
    for snapshot in ({"callExecution": True}, {"kind": "team-role"}):
        with pytest.raises(tasks.TaskError):
            research.validate("web", snapshot)


def test_draft_mode_survives_reopen_and_newer_draft_survives_binding(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        drafts.save(
            store, sid, drafts.Save(expected_revision=0, text="Research this", research_mode="web")
        )
    with closing(Store(path)) as store:
        assert drafts.load(store, sid)["research_mode"] == "web"
        with pytest.raises(submissions.SubmissionError):
            submissions.reserve(
                store, "research_draft_001", sid, "Research this", {}, draft_revision=1
            )
        submissions.reserve(
            store,
            "research_draft_001",
            sid,
            "Research this",
            {},
            draft_revision=1,
            research_mode="web",
        )
        drafts.save(store, sid, drafts.Save(expected_revision=1, text="Next", research_mode="deep"))
        submissions.bind(store, "research_draft_001")
        assert drafts.load(store, sid)["research_mode"] == "deep"
        assert drafts.load(store, sid)["text"] == "Next"


def test_consumed_draft_clears_mode_with_text(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        drafts.save(
            store, sid, drafts.Save(expected_revision=0, text="Research this", research_mode="deep")
        )
        submissions.reserve(
            store,
            "research_draft_002",
            sid,
            "Research this",
            {},
            draft_revision=1,
            research_mode="deep",
        )
        submissions.bind(store, "research_draft_002")
        value = drafts.load(store, sid)
        assert value["text"] == "" and value.get("research_mode", "off") == "off"


def test_http_research_selection_is_forwarded_and_receipt_explains_unavailability(
    tmp_path, monkeypatch
):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        provider = Recorder()
        key = "research-test-gateway"
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "loop", loop_with(store, provider), raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "recovery-only", raising=False)
        monkeypatch.setattr(
            api.app.state,
            "gateway_key_hash",
            hashlib.sha256(key.encode()).hexdigest(),
            raising=False,
        )
        client = TestClient(api.app)
        try:
            payload = {
                "text": "Investigate",
                "request_id": "research_http_001",
                "research_mode": "web",
            }
            assert client.post(f"/sessions/{sid}/turns", json=payload).status_code == 401
            headers = {"X-Pi-Gateway-Key": key}
            response = client.post(f"/sessions/{sid}/turns", json=payload, headers=headers)
            assert response.status_code == 503
            receipt = client.get("/turn-submissions/research_http_001", headers=headers).json()
            assert receipt["research_mode"] == "web"
            assert receipt["failure_code"] == "research_unavailable"
            assert provider.calls == []
        finally:
            client.close()
