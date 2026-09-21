from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime

import pytest

from pi import jobs
from pi.store import Store


def definition(**changes):
    value = dict(
        name="Daily report",
        instructions="Run the published report.",
        agentId="companion",
        timing=dict(kind="interval", time="09:00", day=0, hours=1),
        timeZone="UTC",
        enabled=True,
        target=dict(kind="automation", id="report", publishedVersion=1, digest="a" * 64, args={}),
    )
    value.update(changes)
    return jobs.Definition.model_validate(value)


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "jobs.db")) as store:
        yield store


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def test_weekly_missing_dst_slot_skips_to_next_week():
    d = definition(
        timeZone="America/New_York", timing=dict(kind="weekly", day=0, time="02:30", hours=1)
    )
    start = stamp("2026-03-02T00:00:00+00:00")
    assert jobs.next_due(d, start, anchor=start) == stamp("2026-03-15T06:30:00+00:00")


def test_repeated_dst_hour_occurs_once():
    d = definition(
        timeZone="America/New_York", timing=dict(kind="daily", day=0, time="01:30", hours=1)
    )
    start = stamp("2026-11-01T04:00:00+00:00")
    first = jobs.next_due(d, start, anchor=start)
    assert first == stamp("2026-11-01T05:30:00+00:00")
    assert jobs.next_due(d, first, anchor=start) == stamp("2026-11-02T06:30:00+00:00")


def test_concurrent_claim_and_dispatch_are_once(store):
    job = jobs.create(store, definition(), now=0)
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda _: jobs.claim_due(store, now=3600), range(2)))
    assert sorted(map(len, claims)) == [0, 1]
    run = next(result[0] for result in claims if result)
    calls = []

    def invoke(target, **kwargs):
        calls.append((target, kwargs))
        return {"status": "completed"}

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: jobs.dispatch_claim(store, run, invoke), range(2)))
    assert len(calls) == 1
    assert jobs.runs(store, job["id"])[0]["status"] == "completed"


def test_snapshot_immutable_from_update_or_claim_tampering(store):
    job = jobs.create(store, definition(), now=0)
    run = jobs.claim_due(store, now=3600)[0]
    jobs.update(
        store,
        job["id"],
        jobs.Update(
            expected_revision=1,
            definition=definition(
                target=dict(
                    kind="tool", id="different", publishedVersion=2, digest="b" * 64, args={}
                )
            ),
        ),
        now=3601,
    )
    run["definition"]["target"]["id"] = "tampered"
    captured = []
    jobs.dispatch_claim(
        store, run, lambda target, **kw: captured.append(target) or {"status": "completed"}
    )
    assert captured[0]["id"] == "report"
    assert captured[0]["publishedVersion"] == 1
    with pytest.raises(jobs.JobError, match="changed"):
        jobs.update(store, job["id"], jobs.Update(expected_revision=1, definition=definition()))


def test_uncertain_effect_never_retried_or_overlapped(store):
    job = jobs.create(store, definition(), now=0)
    run = jobs.claim_due(store, now=3600)[0]
    calls = []

    def invoke(*args, **kw):
        calls.append(kw)
        raise TimeoutError("possibly already ran")

    assert jobs.dispatch_claim(store, run, invoke) == "outcome_unknown"
    assert jobs.dispatch_claim(store, run, invoke) == "outcome_unknown"
    assert not jobs.claim_due(store, now=7200)
    with pytest.raises(jobs.JobError, match="Resolve"):
        jobs.run_now(store, job["id"], "new_manual_request", now=7201)
    assert len(calls) == 1


def test_missed_slots_coalesce_and_manual_request_is_idempotent(store):
    job = jobs.create(store, definition(), now=0)
    run = jobs.claim_due(store, now=36000)[0]
    assert run["scheduled_at"] == 3600
    assert jobs.list_jobs(store)[0]["next_at"] == 39600
    jobs.dispatch_claim(store, run, lambda *a, **kw: {"status": "completed"})
    manual = jobs.run_now(store, job["id"], "manual_request_0001", now=36001)
    assert jobs.run_now(store, job["id"], "manual_request_0001", now=36002)["id"] == manual["id"]
    other = jobs.create(store, definition(), now=0)
    with pytest.raises(jobs.JobError, match="different job"):
        jobs.run_now(store, other["id"], "manual_request_0001")


def test_disabled_jobs_and_failed_dispatch_receipt(store):
    job = jobs.create(store, definition(enabled=False), now=0)
    assert not jobs.claim_due(store, now=3600)
    run = jobs.run_now(store, job["id"], "explicit_manual_run", now=3601)
    assert (
        jobs.dispatch_claim(store, run, lambda *a, **kw: {"status": "failed", "reason": "denied"})
        == "failed"
    )
    assert jobs.runs(store, job["id"])[0]["receipt"]["reason"] == "denied"


def test_crash_after_dispatch_admission_stays_held(store):
    job = jobs.create(store, definition(), now=0)
    run = jobs.claim_due(store, now=3600)[0]
    with store._connect() as db:
        db.execute("UPDATE scheduled_runs SET status='dispatching' WHERE id=?", (run["id"],))
    assert (
        jobs.dispatch_claim(store, run, lambda *a, **kw: pytest.fail("must not retry"))
        == "dispatching"
    )
    assert not jobs.claim_due(store, now=7200)
    assert jobs.runs(store, job["id"])[0]["status"] == "dispatching"


def test_restart_preserves_ready_run_and_snapshot(store):
    job = jobs.create(store, definition(), now=0)
    run = jobs.claim_due(store, now=3600)[0]
    path = store.path
    store.close()
    with closing(Store(path)) as reopened:
        assert jobs.runs(reopened, job["id"])[0]["status"] == "ready"
        assert (
            jobs.dispatch_claim(reopened, run, lambda *a, **kw: {"status": "completed"})
            == "completed"
        )


def test_reconciliation_resolves_without_dispatch(store):
    jobs.create(store, definition(), now=0)
    run = jobs.claim_due(store, now=3600)[0]
    jobs.dispatch_claim(store, run, lambda *a, **kw: {"status": "outcome_unknown"})

    class Receipts:
        result = {"status": "outcome_unknown"}
        calls = []

        def reconcile(self, target, **kw):
            self.calls.append((target, kw))
            return self.result

    adapter = Receipts()
    assert jobs.reconcile(store, run["id"], adapter) == "outcome_unknown"
    assert not jobs.claim_due(store, now=7200)
    adapter.result = {"status": "completed", "action_id": run["id"]}
    assert jobs.reconcile(store, run["id"], adapter) == "completed"
    assert jobs.reconcile(store, run["id"], adapter) == "completed"
    assert len(adapter.calls) == 2
    assert adapter.calls[0][1] == {"action_id": run["id"], "agent_id": "companion"}
    assert len(jobs.claim_due(store, now=10800)) == 1


def test_approval_resume_uses_saved_identity_once(store):
    job = jobs.create(store, definition(), now=0)
    run = jobs.claim_due(store, now=3600)[0]
    jobs.dispatch_claim(
        store,
        run,
        lambda *a, **kw: {"status": "awaiting_approval", "request_id": "approval-original"},
    )
    jobs.update(
        store,
        job["id"],
        jobs.Update(expected_revision=1, definition=definition(agentId="changed")),
        now=3601,
    )
    calls = []

    def invoke(target, **kwargs):
        calls.append((target, kwargs))
        return {"status": "completed"}

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: jobs.dispatch_claim(store, run, invoke, resume=True), range(2)))
    assert len(calls) == 1
    assert calls[0][1] == {
        "action_id": run["id"],
        "agent_id": "companion",
        "approval_request_id": "approval-original",
    }
    assert jobs.runs(store, job["id"])[0]["status"] == "completed"


def test_approval_resume_timeout_never_resubmits(store):
    jobs.create(store, definition(), now=0)
    run = jobs.claim_due(store, now=3600)[0]
    jobs.dispatch_claim(
        store,
        run,
        lambda *a, **kw: {"status": "awaiting_approval", "request_id": "approval-original"},
    )
    calls = []

    def timeout(*args, **kwargs):
        calls.append(kwargs)
        raise TimeoutError("possibly executed")

    assert jobs.dispatch_claim(store, run, timeout, resume=True) == "outcome_unknown"
    assert jobs.dispatch_claim(store, run, timeout, resume=True) == "outcome_unknown"
    assert len(calls) == 1


def test_api_authorization_validation_and_receipts(store):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from pi.jobs_api import router

    allowed = False

    def authorize():
        if not allowed:
            raise HTTPException(403)

    app = FastAPI()
    app.include_router(router(lambda: store, authorize))
    with TestClient(app) as client:
        assert client.get("/jobs").status_code == 403
        assert client.post("/jobs/runs/unknown/resume").status_code == 403
        assert client.post("/jobs/runs/unknown/reconcile").status_code == 403
        allowed = True
        assert client.post("/jobs/runs/unknown/resume").status_code == 503
        invalid = definition().model_dump()
        invalid["timeZone"] = "invalid/zone"
        assert client.post("/jobs", json=invalid).status_code == 422
        response = client.post("/jobs", json=definition().model_dump())
        assert response.status_code == 201
        identity = response.json()["id"]
        run = client.post(f"/jobs/{identity}/run", json={"request_id": "owner_manual_request"})
        assert run.status_code == 202
        assert client.get(f"/jobs/{identity}/runs").json()[0]["id"] == run.json()["id"]
        assert (
            client.post(
                f"/jobs/{identity}/update",
                json={"expected_revision": 2, "definition": definition().model_dump()},
            ).status_code
            == 409
        )
