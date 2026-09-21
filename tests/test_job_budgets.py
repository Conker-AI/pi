from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_jobs import definition, store  # noqa: F401

from pi import jobs
from pi.job_execution import PublishedJobs
from pi.job_worker import JobWorker
from pi.jobs_api import router
from pi.toolgate import ToolGateClient

BUDGET = "job_" + "a" * 32


class Adapter:
    def __init__(self):
        self.calls = []
        self.valid = True

    def validate_budget(self, budget_id, **kwargs):
        return self.valid and budget_id == BUDGET

    def __call__(self, target, **kwargs):
        self.calls.append(kwargs)
        return {"status": "awaiting_approval", "request_id": "approval"}


def test_paid_runs_wait_bind_once_and_retain_budget_on_resume(request):
    database = request.getfixturevalue("store")
    job = jobs.create(database, definition(requireBudget=True), now=0)
    adapter = Adapter()
    worker = JobWorker(database, adapter)
    worker.tick(now=3600)
    run = jobs.runs(database, job["id"])[0]
    assert run["status"] == "awaiting_budget" and not adapter.calls
    assert jobs.dispatch_claim(database, run, adapter) == "awaiting_budget"
    body = jobs.BudgetBinding(budget_id=BUDGET)
    adapter.valid = False
    with pytest.raises(jobs.JobError):
        jobs.bind_budget(database, run["id"], body, adapter)
    assert jobs.runs(database, job["id"])[0]["status"] == "awaiting_budget"
    adapter.valid = True
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda _: jobs.bind_budget(database, run["id"], body, adapter), range(2))
        )
    assert sorted(item["replayed"] for item in results) == [False, True]
    assert jobs.runs(database, job["id"])[0]["spending_budget_id"] == BUDGET
    from contextlib import closing

    from pi.store import Store

    with closing(Store(database.path)) as reopened:
        assert jobs.runs(reopened, job["id"])[0]["spending_budget_id"] == BUDGET
    worker.tick(now=3601)
    assert adapter.calls[0]["spending_job_id"] == BUDGET
    assert adapter.calls[0]["action_id"] == run["id"]
    assert jobs.dispatch_claim(database, run, adapter, resume=True) == "awaiting_approval"
    assert adapter.calls[1]["spending_job_id"] == BUDGET
    assert adapter.calls[1]["approval_request_id"] == "approval"
    # Each later occurrence requires its own grant; do not inherit budget from the definition.
    with database._connect() as db:
        db.execute("UPDATE scheduled_runs SET status='completed' WHERE id=?", (run["id"],))
    worker.tick(now=7200)
    later = jobs.runs(database, job["id"])[0]
    assert later["status"] == "awaiting_budget" and len(adapter.calls) == 2
    with pytest.raises(jobs.JobError, match="another run"):
        jobs.bind_budget(database, later["id"], body, adapter)


@pytest.mark.parametrize(
    "root,cap,status", [("run", 10, 200), ("other", 10, 200), ("run", True, 200), ("run", 10, 404)]
)
def test_budget_validation_and_exact_dispatch(monkeypatch, root, cap, status):
    seen = []

    def request(self, gate, method, path, **kwargs):
        seen.append((method, path, kwargs))
        return httpx.Response(status, json={"job_id": BUDGET, "root_action_id": root, "cap": cap})

    monkeypatch.setattr(PublishedJobs, "_request", request)
    adapter = PublishedJobs({"companion": ToolGateClient("http://gate", "scoped")})
    assert adapter.validate_budget(BUDGET, action_id="run", agent_id="companion") is (
        root == "run" and type(cap) is int and status == 200
    )
    assert seen[0][0:2] == ("GET", "/v2/agent/spending/jobs/" + BUDGET)
    adapter(definition().target, action_id="run", agent_id="companion", spending_job_id=BUDGET)
    assert seen[1][2]["json"]["job_id"] == BUDGET
    assert not adapter.validate_budget(BUDGET, action_id="run", agent_id="other")
    assert len(seen) == 2


def test_budget_route_requires_owner_and_valid_shape(request):
    database = request.getfixturevalue("store")
    adapter = Adapter()
    allowed = False

    def authorize():
        if not allowed:
            raise HTTPException(403)

    app = FastAPI()
    app.include_router(router(lambda: database, authorize, lambda: adapter))
    job = jobs.create(database, definition(requireBudget=True), now=0)
    run = jobs.run_now(database, job["id"], "manual_budget_request", now=1)
    with TestClient(app) as client:
        path = f"/jobs/runs/{run['id']}/budget"
        assert client.post(path, json={"budget_id": BUDGET}).status_code == 403
        allowed = True
        assert client.post(path, json={"budget_id": "../wrong"}).status_code == 422
        assert client.post(path, json={"budget_id": BUDGET}).status_code == 200
        assert client.post(path, json={"budget_id": BUDGET}).json()["replayed"]
