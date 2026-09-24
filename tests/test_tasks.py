"""Task metadata never dispatches; its state and evidence survive real SQLite reopen."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi import actions, activity, api, forgetting, tasks
from pi.store import SCHEMA, Store


@pytest.fixture()
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def body(session, **fields):
    return tasks.CreateTask(
        request_id="request_identity_0001",
        session_id=session,
        outcome="Deliver the requested document",
        criteria=["Document reviewed"],
        **fields,
    )


def change(task, status, **fields):
    return tasks.TransitionTask(
        expected_revision=task["revision"],
        status=status,
        note="Owner reviewed the current state",
        **fields,
    )


def edit(task, **fields):
    return tasks.UpdateTask(
        **{
            "expected_revision": task["revision"],
            "outcome": task["outcome"],
            "criteria": [item["text"] for item in task["criteria"]],
            "parent_task_id": task["parent_task_id"],
            "run_ids": task["run_ids"],
            **fields,
        }
    )


def test_create_is_durable_metadata_without_starting_a_turn(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        task = tasks.create(store, body(session))
        assert task["agent_id"] == "companion"
        assert task["status_source"] == "owner" and task["provenance"] == "recorded"
        assert store.turns(session) == [] and store.messages(session) == []
        assert task["revision"] == 1 and task["status"] == "planned"
        assert len(task["changes"]) == 1 and task["changes"][0]["kind"] == "task_created"
    with closing(Store(path)) as reopened:
        assert tasks.get(reopened, task["id"]) == task
        assert tasks.by_request(reopened, body(session).request_id) == task


def test_duplicate_creation_and_conflicting_request_identity_are_atomic(store):
    session = store.create_session()
    request = body(session)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: tasks.create(store, request), range(2)))
    assert results[0] == results[1]
    assert len(tasks.list_tasks(store)["results"]) == 1
    assert len(activity.list_events(store)["results"]) == 1
    with pytest.raises(tasks.TaskError) as failure:
        tasks.create(store, request.model_copy(update={"outcome": "Different request"}))
    assert failure.value.detail["code"] == "request_conflict"
    updated = tasks.update(store, results[0]["id"], edit(results[0], outcome="Revised outcome"))
    assert tasks.create(store, request) == updated


def test_revision_race_keeps_one_change_and_does_not_lose_owner_text(store):
    task = tasks.create(store, body(store.create_session()))

    def save(label):
        try:
            return tasks.update(store, task["id"], edit(task, outcome=label))
        except tasks.TaskError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["First edit", "Second edit"]))
    winners = [result for result in results if isinstance(result, dict)]
    errors = [result for result in results if isinstance(result, tasks.TaskError)]
    assert len(winners) == len(errors) == 1
    assert errors[0].detail["current_revision"] == 2
    assert tasks.get(store, task["id"])["outcome"] == winners[0]["outcome"]
    assert len(activity.list_events(store)["results"]) == 2


def test_review_reopen_and_archive_preserve_criteria_identity(store):
    task = tasks.create(store, body(store.create_session()))
    task = tasks.transition(store, task["id"], change(task, "in_progress"))
    with pytest.raises(tasks.TaskError, match="every current"):
        tasks.transition(store, task["id"], change(task, "completed"))
    ids = [criterion["id"] for criterion in task["criteria"]]
    task = tasks.transition(
        store, task["id"], change(task, "completed", completed_criterion_ids=ids)
    )
    with pytest.raises(tasks.TaskError, match="reopen"):
        tasks.update(store, task["id"], edit(task))
    task = tasks.archive(
        store, task["id"], tasks.ArchiveTask(expected_revision=task["revision"], archived=True)
    )
    with pytest.raises(tasks.TaskError):
        tasks.transition(store, task["id"], change(task, "planned"))
    task = tasks.archive(
        store, task["id"], tasks.ArchiveTask(expected_revision=task["revision"], archived=False)
    )
    task = tasks.transition(store, task["id"], change(task, "planned"))
    assert task["completed_criterion_ids"] == []
    task = tasks.update(store, task["id"], edit(task))
    assert [criterion["id"] for criterion in task["criteria"]] == ids


def test_parent_cycles_foreign_runs_and_active_children_are_rejected(store):
    session = store.create_session()
    parent = tasks.create(store, body(session))
    child = tasks.create(
        store,
        body(session).model_copy(
            update={
                "request_id": "request_identity_0002",
                "parent_task_id": parent["id"],
            }
        ),
    )
    with pytest.raises(tasks.TaskError, match="ancestor"):
        tasks.update(store, parent["id"], edit(parent, parent_task_id=child["id"]))
    with pytest.raises(tasks.TaskError, match="child"):
        tasks.transition(store, parent["id"], change(parent, "cancelled"))
    foreign = store.start_turn(store.create_session())
    with pytest.raises(tasks.TaskError, match="this conversation"):
        tasks.update(store, child["id"], edit(child, run_ids=[foreign]))
    with pytest.raises(tasks.TaskError, match="same conversation"):
        tasks.create(
            store,
            body(store.create_session()).model_copy(
                update={
                    "request_id": "request_identity_0003",
                    "parent_task_id": parent["id"],
                }
            ),
        )
    child = tasks.transition(store, child["id"], change(child, "cancelled"))
    parent = tasks.transition(store, parent["id"], change(parent, "cancelled"))
    with pytest.raises(tasks.TaskError, match="active parent"):
        tasks.transition(store, child["id"], change(child, "planned"))
    assert tasks.get(store, child["id"])["status"] == "cancelled"


def test_link_and_unlink_keep_evidence_and_cancel_does_not_stop_the_turn(store):
    session = store.create_session()
    run = store.start_turn(session)
    task = tasks.create(store, body(session, run_ids=[run]))
    assert activity.get_run(store, run)["task_ids"] == [task["id"]]
    assert "run_started" in {
        event["kind"] for event in activity.list_events(store, task_id=task["id"])["results"]
    }
    task = tasks.update(store, task["id"], edit(task, run_ids=[]))
    assert activity.get_run(store, run)["task_ids"] == []
    events = activity.list_events(store, task_id=task["id"])["results"]
    assert {event["kind"] for event in events} >= {"run_linked", "run_unlinked"}
    assert "run_started" not in {event["kind"] for event in events}
    tasks.transition(store, task["id"], change(task, "cancelled"))
    assert store.get_turn(run)["status"] == "running"
    assert activity.get_run(store, run)["outputs"] == []


def test_task_event_scope_excludes_other_tasks_link_history_on_a_shared_run(store):
    session = store.create_session()
    run = store.start_turn(session)
    first = tasks.create(store, body(session, run_ids=[run]))
    second = tasks.create(
        store,
        body(session, run_ids=[run]).model_copy(
            update={
                "request_id": "request_identity_0002",
            }
        ),
    )
    tasks.update(store, second["id"], edit(second, run_ids=[]))
    events, cursor = [], None
    while True:
        page = activity.list_events(store, limit=1, cursor=cursor, task_id=first["id"])
        events.extend(page["results"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert {event["kind"] for event in events} == {
        "task_created",
        "run_linked",
        "run_started",
    }
    assert all(event["task_id"] in (None, first["id"]) for event in events)
    assert len(events) == len({event["sequence"] for event in events}) == 3
    assert {
        event["kind"] for event in activity.list_events(store, task_id=second["id"])["results"]
    } == {"task_created", "run_linked", "task_updated", "run_unlinked"}


def test_task_write_and_event_rollback_together(store):
    session = store.create_session()
    task = tasks.create(store, body(session))
    run = store.start_turn(session)
    with store._connect() as db:
        db.executescript(
            "CREATE TRIGGER reject_test_link BEFORE INSERT ON task_runs "
            "BEGIN SELECT RAISE(ABORT,'test link failed'); END;"
        )
    with pytest.raises(sqlite3.IntegrityError, match="test link failed"):
        tasks.update(store, task["id"], edit(task, outcome="must roll back", run_ids=[run]))
    assert tasks.get(store, task["id"]) == task


def test_events_are_database_append_only_and_do_not_copy_private_content(store):
    session = store.create_session(title="SECRET")
    task = tasks.create(store, body(session).model_copy(update={"outcome": "SECRET"}))
    event = activity.list_events(store)["results"][0]
    assert "SECRET" not in json.dumps(event)
    with store._connect() as db:
        for statement, values in [
            ("UPDATE activity_events SET kind='forged' WHERE id=?", (event["id"],)),
            ("DELETE FROM activity_events WHERE id=?", (event["id"],)),
            (
                (
                    "INSERT OR REPLACE INTO activity_events(id,kind,session_id,occurred_at) "
                    "VALUES(?,'forged',?,0)"
                ),
                (event["id"], session),
            ),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(statement, values)
        with pytest.raises(sqlite3.IntegrityError, match="fixed"):
            db.execute(
                "UPDATE tasks SET session_id=? WHERE id=?", (store.create_session(), task["id"])
            )


def test_run_events_preserve_approval_recovery_and_uncertain_outcomes(store):
    session = store.create_session()
    run = store.start_turn(session)
    actions.prepare(store, run, "a_tool", {"secret": "DO NOT COPY"}, "action_one")
    actions.state(store, "action_one", "awaiting_approval")
    store.finish_turn(run, "awaiting_approval", approval_intent="DO NOT COPY")
    assert store.mark_interrupted_turns() == 0
    assert store.claim_turn(run, "awaiting_approval")
    actions.state(store, "action_one", "outcome_unknown")
    assert store.mark_interrupted_turns() == 1
    observed = activity.get_run(store, run)
    assert observed["status"] == "outcome_unknown"
    assert observed["action"] == {"id": "action_one", "state": "outcome_unknown", "job_id": None}
    events = activity.list_events(store, run_id=run)["results"]
    states = [event["to_status"] for event in reversed(events)]
    assert states == [
        "running",
        "dispatching",
        "awaiting_approval",
        "awaiting_approval",
        "running",
        "outcome_unknown",
        "outcome_unknown",
    ]
    assert "DO NOT COPY" not in json.dumps(events)
    assert "DO NOT COPY" not in json.dumps(observed)


def test_old_database_upgrades_without_fabricating_historical_events(tmp_path):
    path = tmp_path / "pi.db"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO sessions(id,created_at) VALUES('old_session',1)")
        db.execute(
            "INSERT INTO turns(id,session_id,status,started_at,ended_at) "
            "VALUES('old_turn','old_session','complete',1,2)"
        )
    with closing(Store(path)) as store:
        assert activity.get_run(store, "old_turn")["status"] == "complete"
        assert activity.list_events(store)["results"] == []
        task = tasks.create(store, body("old_session", run_ids=["old_turn"]))
    with closing(Store(path)) as store:
        assert tasks.get(store, task["id"])["run_ids"] == ["old_turn"]
        assert len(activity.list_events(store)["results"]) == 2


def test_keyset_pages_have_no_duplicates_and_task_history_is_bounded(store):
    session = store.create_session()
    task = tasks.create(store, body(session))
    for index in range(105):
        task = tasks.update(store, task["id"], edit(task, outcome=f"Revision {index}"))
    assert task["changes_truncated"] and len(task["changes"]) == 100
    sequences, cursor = [], None
    while True:
        page = activity.list_events(store, limit=17, cursor=cursor, task_id=task["id"])
        sequences.extend(event["sequence"] for event in page["results"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(sequences) == len(set(sequences)) == 106
    with pytest.raises(tasks.TaskError, match="cursor"):
        activity.list_events(store, cursor="not-a-cursor")


def test_forgetting_scrubs_tasks_idempotency_hashes_and_views_across_descendants(tmp_path):
    path = tmp_path / "pi.db"
    secret = "private-task-7b14-никому-не-говори"
    with closing(Store(path)) as store:
        root = store.create_session()
        child = store.create_session(parent_id=root)
        other = store.create_session()
        saved = []
        for index, session in enumerate((root, child, other)):
            text = secret if index < 2 else "unrelated safe text"
            run = store.start_turn(session)
            request = body(session).model_copy(
                update={
                    "request_id": f"request_identity_00{index}",
                    "outcome": text,
                    "criteria": [text],
                    "run_ids": [run],
                }
            )
            task = tasks.create(store, request)
            task = tasks.transition(
                store,
                task["id"],
                tasks.TransitionTask(expected_revision=1, status="blocked", note=text),
            )
            saved.append((request, task, run))
        with store._connect() as db:
            hashes = [
                row[0]
                for row in db.execute(
                    "SELECT payload_hash FROM task_requests WHERE task_id!=?", (saved[2][1]["id"],)
                )
            ]
    receipt = forgetting.forget(path, root, forgetting.preview(path, root)["confirmation"])
    assert secret not in json.dumps(receipt)
    with closing(Store(path)) as store:
        for request, original, run in saved[:2]:
            tombstone = tasks.get(store, original["id"])
            assert tombstone["content_status"] == "forgotten"
            assert tombstone["outcome"] == tombstone["status_note"] == ""
            assert tombstone["criteria"] == [] and tombstone["run_ids"] == [run]
            assert secret not in json.dumps(tombstone)
            assert tasks.by_request(store, request.request_id) == tombstone
            with pytest.raises(tasks.TaskError, match="already used"):
                tasks.create(store, request)
            with pytest.raises(tasks.TaskError, match="unavailable"):
                tasks.update(store, original["id"], edit(original))
            assert activity.get_run(store, run)["content_status"] == "forgotten"
        assert tasks.get(store, saved[2][1]["id"])["outcome"] == "unrelated safe text"
        events = activity.list_events(store)["results"]
        assert secret not in json.dumps(events)
        with store._connect() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM task_requests WHERE payload_hash IS NULL"
                ).fetchone()[0]
                == 2
            )
    for entry in path.parent.glob("pi.db*"):
        assert secret.encode() not in entry.read_bytes()
        assert all(value.encode() not in entry.read_bytes() for value in hashes)


def test_strict_metadata_models_reject_fake_agents_and_execution_flags(store):
    request = body(store.create_session()).model_dump()
    for extra in ({"agent_id": "fixture-specialist"}, {"execute": True}, {"status": "completed"}):
        with pytest.raises(ValidationError):
            tasks.CreateTask(**{**request, **extra})
    for change_data in (
        {"criteria": ["Same", "same"]},
        {"outcome": " "},
        {"criteria": ["x" * 501]},
        {"criteria": []},
    ):
        with pytest.raises(ValidationError):
            tasks.CreateTask(**{**request, **change_data})
    with pytest.raises(ValidationError):
        tasks.ArchiveTask(expected_revision=True, archived=True)


def test_authenticated_api_contract_and_recovery_read(monkeypatch, store):
    key = "runtime_key_for_ledger_test"
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "separate_recovery_key", raising=False)
    monkeypatch.setattr(
        api.app.state, "gateway_key_hash", hashlib.sha256(key.encode()).hexdigest(), raising=False
    )
    client = TestClient(api.app)
    try:
        session = store.create_session()
        payload = body(session).model_dump()
        assert client.post("/tasks", json=payload).status_code == 401
        headers = {"X-Pi-Gateway-Key": key}
        response = client.post("/tasks", json=payload, headers=headers)
        assert response.status_code == 200
        task = response.json()
        assert (
            client.get("/tasks/requests/" + payload["request_id"], headers=headers).json() == task
        )
        listed = client.get("/tasks?session_id=" + session, headers=headers).json()
        assert listed["results"] == [task]
        bad = client.post(
            f"/tasks/{task['id']}/update",
            headers=headers,
            json={
                **edit(task).model_dump(),
                "expected_revision": 7,
            },
        )
        assert bad.status_code == 409
        assert bad.json()["detail"]["current_revision"] == 1
        assert client.post(f"/tasks/{task['id']}/run", json={}, headers=headers).status_code == 404
        assert client.get("/runs", headers=headers).json() == {"results": [], "next_cursor": None}
        assert client.get("/events", headers=headers).json()["results"][0]["kind"] == "task_created"
        assert client.get("/events?limit=201", headers=headers).status_code == 422
    finally:
        client.close()
