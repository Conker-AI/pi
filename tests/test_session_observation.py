"""External work is observable without loading content or advancing a write receipt."""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager

import pytest
from fastapi.testclient import TestClient
from test_gateway_api import RUNTIME, gateway, sign_in  # noqa: F401
from test_loop import Recorder, loop_with
from test_response_versions import alternative, choose

from pi.actions import identities as actions
from pi.api import browser_contract
from pi.api import server as api
from pi.conversation import forgetting, submissions
from pi.conversation import observation as session_observation
from pi.conversation.store import Store


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def observe(store, sid):
    return session_observation.read(store, sid)


def test_idle_fast_complete_and_other_session_isolation(store):
    sid, other = store.create_session(title="Private title"), store.create_session()
    before, untouched = observe(store, sid), observe(store, other)
    assert before == {
        "schemaVersion": 1,
        "session_id": sid,
        "revision": 1,
        "active": False,
        "content_status": "available",
    }
    provider = Recorder("Private answer")
    loop_with(store, provider).run_turn(sid, "Private request")
    # Both observations can be idle; the counter still detects the intervening turn.
    after = observe(store, sid)
    assert not after["active"] and after["revision"] > before["revision"]
    assert observe(store, other) == untouched
    assert "Private" not in json.dumps(after)


def test_messages_same_status_turn_update_resume_and_session_close(store):
    sid = store.create_session()
    previous = observe(store, sid)["revision"]
    store.append_message(sid, "user", "Independent message")
    assert observe(store, sid)["revision"] > previous
    tid = store.start_turn(sid)
    running = observe(store, sid)
    assert running["active"]
    with store._connect() as db:
        db.execute("UPDATE turns SET detail='Private intermediate detail' WHERE id=?", (tid,))
    assert observe(store, sid)["revision"] > running["revision"]
    store.finish_turn(tid, "acted_no_reply", acted=1)
    parked = observe(store, sid)
    assert not parked["active"]
    assert store.claim_turn(tid, "acted_no_reply")
    assert observe(store, sid)["active"]
    assert observe(store, sid)["revision"] > parked["revision"]
    store.finish_turn(tid, "complete")
    previous = observe(store, sid)["revision"]
    store.close_session(sid, "closed")
    assert observe(store, sid)["revision"] > previous


@pytest.mark.parametrize(
    "status",
    [
        "awaiting_approval",
        "awaiting_budget",
        "action_in_progress",
        "outcome_unknown",
        "interrupted",
        "failed",
        "cancelled",
    ],
)
def test_parked_unknown_and_terminal_turns_are_not_processing(store, status):
    sid = store.create_session()
    tid = store.start_turn(sid)
    store.finish_turn(tid, status, acted=1)
    assert not observe(store, sid)["active"]


def test_preturn_preparation_failure_replay_and_restart_recovery(store):
    sid = store.create_session()
    before = observe(store, sid)
    args = (store, "external_prepare_01", sid, "Private text", None)
    submissions.reserve(*args)
    preparing = observe(store, sid)
    assert preparing["active"] and preparing["revision"] > before["revision"]
    assert store.turns(sid) == [] and store.messages(sid) == []
    assert submissions.reserve(*args)[1] is False
    assert observe(store, sid) == preparing
    submissions.fail_preparation(store, "external_prepare_01")
    failed = observe(store, sid)
    assert not failed["active"] and failed["revision"] > preparing["revision"]
    submissions.reserve(store, "external_prepare_02", sid, "Other", None)
    before = observe(store, sid)
    with store._connect() as db:
        submissions.recover_preparations(db)
    assert not observe(store, sid)["active"]
    assert observe(store, sid)["revision"] > before["revision"]


def test_forked_submission_updates_source_and_effective_conversation(store):
    sid = store.create_session()
    submissions.reserve(store, "external_fork_0001", sid, "Private text", None)
    before = observe(store, sid)
    bound = submissions.bind(store, "external_fork_0001", fork_summary="Private summary")
    child, tid = bound["effective_session_id"], bound["turn_id"]
    assert child != sid and store.get_session(sid)["status"] == "forked"
    source, destination = observe(store, sid), observe(store, child)
    assert source["active"] and destination["active"]
    assert source["revision"] > before["revision"]
    store.complete_turn(tid, "Final reply")
    assert not observe(store, sid)["active"] and not observe(store, child)["active"]
    assert observe(store, sid)["revision"] > source["revision"]
    assert observe(store, child)["revision"] > destination["revision"]
    assert submissions.list_pending(store, sid)["results"] == []


def test_tool_action_and_thinking_transitions_are_observed(store):
    sid = store.create_session()
    tid = store.start_turn(sid)
    before = observe(store, sid)
    actions.prepare(store, tid, "synthetic.read", {"private": "args"}, "action_observe_01")
    assert observe(store, sid)["revision"] > before["revision"]
    before = observe(store, sid)
    actions.state(store, "action_observe_01", "outcome_unknown")
    assert observe(store, sid)["revision"] > before["revision"]
    store.finish_turn(tid, "complete")
    with store._connect() as db:
        db.execute(
            "INSERT INTO background_thinking(turn_id,session_id,status,created_at) VALUES(?,?,'queued',0)",
            (tid, sid),
        )
    queued = observe(store, sid)
    assert queued["active"]
    for status in ("running", "completed", "canceled"):
        before = observe(store, sid)
        with store._connect() as db:
            db.execute(
                "UPDATE background_thinking SET status=?,result='Private finding' WHERE turn_id=?",
                (status, tid),
            )
        after = observe(store, sid)
        assert after["revision"] > before["revision"]
        assert after["active"] is (status == "running")
        assert "Private" not in json.dumps(after)
    before = observe(store, sid)
    with store._connect() as db:
        db.execute("DELETE FROM background_thinking WHERE turn_id=?", (tid,))
    assert observe(store, sid)["revision"] > before["revision"]


def test_response_selection_updates_revision_without_new_messages(store):
    sid = store.create_session()
    original = loop_with(store, Recorder("Original")).run_turn(sid, "Question")["message"]
    root, replacement = alternative(store, sid, original["id"])
    before, message_count = observe(store, sid), len(store.messages(sid))
    choose(store, sid, root, replacement["id"])
    assert observe(store, sid)["revision"] > before["revision"]
    assert len(store.messages(sid)) == message_count
    assert not observe(store, sid)["active"]


def test_revision_migration_existing_sessions_and_repeated_startup(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as original:
        sid = original.create_session()
        original.append_message(sid, "user", "Old private content")
    # Simulate a prior release, retaining all actual history and authority tables.
    with sqlite3.connect(path) as db:
        names = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'session_observation_%'"
            )
        ]
        for name in names:
            db.execute(f'DROP TRIGGER "{name}"')
        db.execute("DROP TABLE session_observations")
    with closing(Store(path)) as migrated:
        assert observe(migrated, sid)["revision"] == 0
        migrated.append_message(sid, "user", "New content")
        expected = observe(migrated, sid)
        assert expected["revision"] > 0
        with migrated._connect() as db:
            session_observation.initialize(db)
        assert observe(migrated, sid) == expected
    with closing(Store(path)) as reopened:
        assert observe(reopened, sid) == expected
        assert len(reopened.messages(sid)) == 2


def test_reads_are_content_free_and_do_not_write_or_load_history(store, monkeypatch):
    sid = store.create_session()
    store.append_message(sid, "user", "Private content")

    def forbidden(*args, **kwargs):
        pytest.fail("Observation must not load a transcript or model")

    monkeypatch.setattr(store, "messages", forbidden)
    monkeypatch.setattr(store, "turns", forbidden)
    with store._connect() as monitor:
        version = monitor.execute("PRAGMA data_version").fetchone()[0]
        baseline = observe(store, sid)
        for _ in range(4):
            assert observe(store, sid) == baseline
        assert monitor.execute("PRAGMA data_version").fetchone()[0] == version


def test_snapshot_revision_and_active_are_coherent_during_concurrent_commit(store, monkeypatch):
    sid = store.create_session()
    baseline = observe(store, sid)
    connect = store._connect
    changed = False

    class Connection:
        def __init__(self, db):
            self.db = db

        def execute(self, statement, *args):
            nonlocal changed
            cursor = self.db.execute(statement, *args)
            if statement.startswith("SELECT s.status") and not changed:
                changed = True
                with connect() as writer:
                    writer.execute(
                        "INSERT INTO turns(id,session_id,status,started_at) VALUES('trn_concurrent',?,'running',0)",
                        (sid,),
                    )
            return cursor

    @contextmanager
    def hooked():
        with connect() as db:
            yield Connection(db)

    monkeypatch.setattr(store, "_connect", hooked)
    assert observe(store, sid) == baseline
    following = observe(store, sid)
    assert following["active"] and following["revision"] > baseline["revision"]


def test_transaction_rollback_does_not_publish_revision(store):
    sid = store.create_session()
    before = observe(store, sid)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO turns(id,session_id,status,started_at) VALUES('trn_rollback',?,'running',0)",
            (sid,),
        )
        db.rollback()
    assert observe(store, sid) == before


@pytest.mark.parametrize("populated", [False, True])
def test_offline_forget_advances_and_masks_observation(store, populated):
    sid = store.create_session(title="Private title")
    if populated:
        loop_with(store, Recorder("Private answer")).run_turn(sid, "Private input")
    before = observe(store, sid)
    plan = forgetting.preview(store.path, sid)
    store.close()
    forgetting.forget(store.path, sid, plan["confirmation"])
    with closing(Store(store.path)) as reopened:
        after = observe(reopened, sid)
        assert after["revision"] > before["revision"]
        assert after["content_status"] == "forgotten" and not after["active"]
        assert "Private" not in json.dumps(after)


def test_independent_forget_tombstone_masks_running_legacy_rows(store):
    sid = store.create_session()
    store.start_turn(sid)
    before = observe(store, sid)
    with store._connect() as db:
        db.execute("INSERT INTO forgetting_receipts VALUES('del_test',?,0,'confirmed',0,1)", (sid,))
        db.execute("INSERT INTO forgotten_sessions VALUES(?,'del_test')", (sid,))
    after = observe(store, sid)
    assert after["revision"] > before["revision"]
    assert after["content_status"] == "forgotten" and not after["active"]


def test_api_auth_query_identity_bounds_and_safe_revision(store, monkeypatch):
    sid = store.create_session()
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "recovery-key", raising=False)
    monkeypatch.setattr(
        api.app.state, "gateway_key_hash", hashlib.sha256(b"runtime-key").hexdigest(), raising=False
    )
    client = TestClient(api.app)
    headers = {"X-Pi-Gateway-Key": "runtime-key"}
    path = f"/sessions/{sid}/observation"
    assert browser_contract.runtime_allowed("GET", path)
    assert not browser_contract.runtime_allowed("POST", path)
    assert client.get(path).status_code == 401
    result = client.get(path, headers=headers)
    assert result.json() == observe(store, sid)
    assert result.headers["Cache-Control"] == "no-store"
    assert client.get(path, headers={"X-Pi-Key": "recovery-key"}).json() == result.json()
    for suffix in ("?text=private", "?limit=1", "?revision=1&revision=2"):
        assert client.get(path + suffix, headers=headers).status_code == 422
    assert client.get("/sessions/missing/observation", headers=headers).status_code == 404
    for identity in ("bad.id", "a" * 129, "日本語"):
        assert (
            client.get(
                f"/sessions/{identity}/observation", headers={"X-Pi-Key": "recovery-key"}
            ).status_code
            == 422
        )
    with store._connect() as db:
        db.execute(
            "UPDATE session_observations SET revision=? WHERE session_id=?",
            (session_observation.MAX_SAFE_REVISION + 1, sid),
        )
    assert client.get(path, headers=headers).status_code == 503


def test_gateway_uses_scoped_runtime_credential_and_never_forwards_browser_keys(gateway):
    client, _, seen = gateway
    path = "/api/pi/sessions/ses_synthetic/observation"
    assert client.get(path).status_code == 401
    assert not seen
    sign_in(client)
    response = client.get(path, headers={"X-Pi-Key": "attacker", "X-Pi-Gateway-Key": "attacker"})
    assert response.status_code == 200
    assert len(seen) == 1
    assert seen[0].headers["X-Pi-Gateway-Key"] == RUNTIME
    assert "X-Pi-Key" not in seen[0].headers
    assert "Set-Cookie" not in response.headers and response.headers["Cache-Control"] == "no-store"
    assert client.post(path, json={}, headers=sign_in(client)).status_code == 403
    assert len(seen) == 1


def test_idle_queries_have_partial_active_indexes(store):
    with store._connect() as db:
        queries = [
            (
                "SELECT 1 FROM background_thinking WHERE session_id=? AND status IN ('queued','running')",
                "observation_active_thinking",
            ),
            (
                "SELECT 1 FROM turns WHERE session_id=? AND status='running'",
                "observation_running_turns",
            ),
        ]
        for query, index in queries:
            plan = " ".join(
                str(tuple(row)) for row in db.execute("EXPLAIN QUERY PLAN " + query, ("missing",))
            )
            assert index in plan
