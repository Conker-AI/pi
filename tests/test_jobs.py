import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from pi import api, jobs
from pi.browser_contract import owner_allowed, runtime_allowed
from pi.store import Store

OWNER = "jobs-owner-control-key-" + "o" * 32


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
        assert client.post("/jobs/runs/unknown/resume").status_code == 422
        missing = "scheduled_" + "0" * 32
        assert client.post(f"/jobs/runs/{missing}/resume").status_code == 503
        invalid = definition().model_dump()
        invalid["timeZone"] = "invalid/zone"
        assert client.post("/jobs", json=invalid).status_code == 422
        response = client.post("/jobs", json=definition().model_dump())
        assert response.status_code == 201
        identity = response.json()["id"]
        run = client.post(f"/jobs/{identity}/run", json={"request_id": "owner_manual_request"})
        assert run.status_code == 202
        assert client.get(f"/jobs/{identity}/runs").json()["results"][0]["id"] == run.json()["id"]
        assert (
            client.post(
                f"/jobs/{identity}/update",
                json={"expected_revision": 2, "definition": definition().model_dump()},
            ).status_code
            == 409
        )


def test_connected_owner_schedule_contract_is_redacted_bounded_and_restart_safe(
    tmp_path, monkeypatch
):
    path = tmp_path / "owner-jobs.db"
    store = Store(path)
    seeded = definition(
        instructions="Run the published report with never-return-instructions.",
        enabled=False,
        target={
            "kind": "automation",
            "id": "report",
            "publishedVersion": 1,
            "digest": "a" * 64,
            "args": {"credential": "never-return-this", "path": "C:\\private\\input.txt"},
        },
    )
    job = jobs.create(store, seeded, now=100)
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "jobs-admin-" + "a" * 32, raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    monkeypatch.setattr(
        api.app.state,
        "gateway_key_hash",
        hashlib.sha256(("r" * 32).encode()).hexdigest(),
        raising=False,
    )
    monkeypatch.setattr(api.app.state, "job_executor", None, raising=False)
    headers = {"X-Pi-Owner-Key": OWNER}
    client = TestClient(api.app)

    assert client.get("/jobs").status_code == 401
    assert client.get("/jobs", headers={"X-Pi-Gateway-Key": "r" * 32}).status_code == 401
    assert client.post("/jobs", headers=headers, json=seeded.model_dump()).status_code == 403
    assert (
        client.post(
            f"/jobs/{job['id']}/update",
            headers=headers,
            json={"expected_revision": 1, "definition": seeded.model_dump()},
        ).status_code
        == 403
    )

    listing = client.get("/jobs?limit=1", headers=headers)
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["schemaVersion"] == 1 and payload["nextCursor"] is None
    view = payload["results"][0]
    assert view["authority"] == "none" and view["execution"] == "not-triggered"
    assert view["definition"]["state"] == "paused"
    assert view["definition"]["target"]["inputsConfigured"] is True
    assert view["definition"]["instructionsConfigured"] is True
    assert "instructions" not in view["definition"]
    assert "args" not in view["definition"]["target"]
    assert "never-return" not in listing.text and "private" not in listing.text

    enabled = client.post(
        f"/jobs/{job['id']}/state",
        headers=headers,
        json={"expected_revision": 1, "enabled": True},
    )
    assert enabled.status_code == 200
    assert enabled.json()["revision"] == 2
    assert enabled.json()["definition"]["state"] == "enabled"
    assert enabled.json()["execution"] == "not-triggered"
    assert (
        client.post(
            f"/jobs/{job['id']}/state",
            headers=headers,
            json={"expected_revision": 1, "enabled": False},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/jobs/{job['id']}/state",
            headers=headers,
            json={"expected_revision": 2, "enabled": True, "credential": "forbidden"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/jobs/{job['id']}/run",
            headers=headers,
            json={"request_id": "contains whitespace"},
        ).status_code
        == 422
    )

    request_id = "owner_manual_run_0001"
    first = client.post(f"/jobs/{job['id']}/run", headers=headers, json={"request_id": request_id})
    assert first.status_code == 202
    assert first.json()["status"] == "ready"
    assert first.json()["execution"] == "admitted-only"
    assert first.json()["replayed"] is False
    repeated = client.post(
        f"/jobs/{job['id']}/run", headers=headers, json={"request_id": request_id}
    )
    assert repeated.status_code == 202 and repeated.json()["replayed"] is True
    run_id = first.json()["id"]
    cancelled = client.post(f"/jobs/runs/{run_id}/cancel", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert client.post(f"/jobs/runs/{run_id}/cancel", headers=headers).json()["replayed"] is True

    class Executor:
        calls = []

        def __call__(self, target, **kwargs):
            self.calls.append((target, kwargs))
            return {"status": "completed", "reason": "never-return-receipt"}

        def reconcile(self, target, **kwargs):
            self.calls.append((target, kwargs))
            return {"status": "completed", "code": "OK", "detail": "never-return-detail"}

        def validate_budget(self, budget_id, **kwargs):
            self.calls.append((budget_id, kwargs))
            return True

    executor = Executor()
    monkeypatch.setattr(api.app.state, "job_executor", executor, raising=False)
    approval = jobs.run_now(store, job["id"], "owner_approval_run_01", now=102)
    jobs.dispatch_claim(
        store,
        approval,
        lambda *args, **kwargs: {
            "status": "awaiting_approval",
            "request_id": "approval-secret-id",
        },
    )
    resumed = client.post(f"/jobs/runs/{approval['id']}/resume", headers=headers)
    assert resumed.status_code == 200 and resumed.json()["status"] == "completed"
    assert "approval-secret-id" not in resumed.text and "never-return" not in resumed.text

    unknown = jobs.run_now(store, job["id"], "owner_unknown_run_001", now=103)
    jobs.dispatch_claim(store, unknown, lambda *args, **kwargs: {"status": "outcome_unknown"})
    reconciled = client.post(f"/jobs/runs/{unknown['id']}/reconcile", headers=headers)
    assert reconciled.status_code == 200 and reconciled.json()["status"] == "completed"
    assert reconciled.json()["outcomeCode"] == "OK"
    assert "never-return-detail" not in reconciled.text

    budget_job = jobs.create(store, definition(name="Budgeted", requireBudget=True), now=104)
    budget_run = jobs.run_now(store, budget_job["id"], "owner_budget_run_0001", now=105)
    budget_id = "job_" + "c" * 32
    bound = client.post(
        f"/jobs/runs/{budget_run['id']}/budget",
        headers=headers,
        json={"budget_id": budget_id},
    )
    assert bound.status_code == 200 and bound.json()["budgetBound"] is True
    assert budget_id not in bound.text
    jobs_page = client.get("/jobs?limit=1", headers=headers).json()
    assert jobs_page["nextCursor"] == jobs_page["results"][0]["id"]
    jobs_next = client.get(
        "/jobs", headers=headers, params={"limit": 1, "cursor": jobs_page["nextCursor"]}
    ).json()
    assert len(jobs_next["results"]) == 1
    assert jobs_next["results"][0]["id"] != jobs_page["results"][0]["id"]

    history = client.get(f"/jobs/{job['id']}/runs?limit=1", headers=headers).json()
    assert history["schemaVersion"] == 1 and len(history["results"]) == 1
    assert history["nextCursor"] == history["results"][0]["id"]
    next_history = client.get(
        f"/jobs/{job['id']}/runs",
        headers=headers,
        params={"limit": 1, "cursor": history["nextCursor"]},
    ).json()
    assert len(next_history["results"]) == 1
    assert "definition" not in history["results"][0]
    assert "receipt" not in history["results"][0]
    assert "request_id" not in history["results"][0]
    assert client.get("/jobs/job_short", headers=headers).status_code == 403
    monkeypatch.setattr(api.app.state, "job_executor", None, raising=False)
    assert client.post(f"/jobs/runs/{run_id}/provision-budget", headers=headers).status_code == 503

    store.close()
    reopened = Store(path)
    monkeypatch.setattr(api.app.state, "store", reopened, raising=False)
    try:
        saved = client.get(f"/jobs/{job['id']}", headers=headers)
        assert saved.status_code == 200 and saved.json()["revision"] == 2
        saved_history = client.get(f"/jobs/{job['id']}/runs", headers=headers).json()
        assert {row["status"] for row in saved_history["results"]} == {"cancelled", "completed"}
    finally:
        reopened.close()


def test_owner_job_browser_allowlist_is_exact():
    job_id = "job_" + "a" * 32
    run_id = "scheduled_" + "b" * 32
    for path in ("/jobs", f"/jobs/{job_id}", f"/jobs/{job_id}/runs"):
        assert owner_allowed("GET", path)
        assert not runtime_allowed("GET", path)
    for path in (
        f"/jobs/{job_id}/state",
        f"/jobs/{job_id}/run",
        f"/jobs/runs/{run_id}/budget",
        f"/jobs/runs/{run_id}/provision-budget",
        f"/jobs/runs/{run_id}/cancel",
        f"/jobs/runs/{run_id}/resume",
        f"/jobs/runs/{run_id}/reconcile",
    ):
        assert owner_allowed("POST", path)
        assert not runtime_allowed("POST", path)
    for method, path in (
        ("POST", "/jobs"),
        ("POST", f"/jobs/{job_id}/update"),
        ("DELETE", f"/jobs/{job_id}"),
        ("GET", "/jobs/job_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        ("POST", f"/jobs/runs/{run_id}/dispatch"),
        ("GET", f"/jobs/{job_id}/runs/extra"),
    ):
        assert not owner_allowed(method, path)
