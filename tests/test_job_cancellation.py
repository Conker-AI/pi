from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_jobs import definition

from pi import continuity, jobs
from pi.jobs_api import router
from pi.store import Store


@pytest.mark.parametrize("state", ["ready", "awaiting_budget", "awaiting_approval"])
def test_waiting_run_cancels_durably_and_releases_overlap(tmp_path, state):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        job = jobs.create(store, definition(requireBudget=state == "awaiting_budget"), now=0)
        run = jobs.run_now(store, job["id"], "cancel_waiting_request", now=1)
        if state == "awaiting_approval":
            jobs.dispatch_claim(
                store,
                run,
                lambda *a, **kw: {
                    "status": "awaiting_approval",
                    "request_id": "retained-approval",
                },
            )
        assert jobs.cancel_run(store, run["id"]) == {"status": "cancelled", "replayed": False}
        assert jobs.cancel_run(store, run["id"])["replayed"]
        assert (
            jobs.dispatch_claim(store, run, lambda *a, **kw: pytest.fail("cancelled"))
            == "cancelled"
        )
        assert (
            jobs.dispatch_claim(store, run, lambda *a, **kw: pytest.fail("resume"), resume=True)
            == "cancelled"
        )
        row = jobs.runs(store, job["id"])[0]
        if state == "awaiting_approval":
            assert row["receipt"]["request_id"] == "retained-approval"
        item = continuity.briefing(store)["items"][0]
        assert item["status"] == "cancelled" and not item["needsAttention"]
    with closing(Store(path)) as store:
        assert jobs.runs(store, job["id"])[0]["status"] == "cancelled"
        replacement = jobs.run_now(store, job["id"], "after_cancel_request", now=2)
        assert replacement["id"] != run["id"]
        replay = jobs.run_now(store, job["id"], "cancel_waiting_request", now=3)
        assert replay["status"] == "cancelled" and replay["replayed"]


def test_dispatch_claim_wins_before_cancel_and_unknown_is_not_cancelled(tmp_path):
    entered, release = Event(), Event()
    with closing(Store(tmp_path / "pi.db")) as store:
        job = jobs.create(store, definition(), now=0)
        run = jobs.run_now(store, job["id"], "dispatch_cancel_race", now=1)

        def invoke(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return {"status": "outcome_unknown"}

        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(jobs.dispatch_claim, store, run, invoke)
            try:
                assert entered.wait(5)
                with pytest.raises(jobs.JobError, match="reconcile"):
                    jobs.cancel_run(store, run["id"])
            finally:
                release.set()
            assert future.result() == "outcome_unknown"
        with pytest.raises(jobs.JobError, match="reconcile"):
            jobs.cancel_run(store, run["id"])
        assert jobs.runs(store, job["id"])[0]["status"] == "outcome_unknown"
        with pytest.raises(jobs.JobError):
            jobs.run_now(store, job["id"], "overlap_still_blocked", now=2)


def test_cancel_route_is_owner_only_without_execution_adapter(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        allowed = False

        def authorize():
            if not allowed:
                raise HTTPException(403)

        app = FastAPI()
        app.include_router(router(lambda: store, authorize))
        job = jobs.create(store, definition(requireBudget=True), now=0)
        run = jobs.run_now(store, job["id"], "cancel_route_request", now=1)
        with TestClient(app) as client:
            path = f"/jobs/runs/{run['id']}/cancel"
            assert client.post(path).status_code == 403
            allowed = True
            assert client.post(path).json()["status"] == "cancelled"
            assert client.post(path).json()["replayed"]
