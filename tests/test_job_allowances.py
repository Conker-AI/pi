import pytest
from test_jobs import definition, store  # noqa: F401
from test_job_budgets import Adapter, BUDGET
from pi import jobs
from pi.job_worker import JobWorker

ALLOWANCE = "allowance_" + "b" * 32


class Allocator(Adapter):
    def __init__(self):
        super().__init__()
        self.allocations = []
        self.fail = False

    def allocate_budget(self, allowance, **kwargs):
        self.allocations.append((allowance, kwargs))
        if self.fail:
            raise ValueError("private transport details")
        return BUDGET


def test_lost_reply_retries_same_root_before_dispatch(store):
    job = jobs.create(store, definition(requireBudget=True, budgetAllowanceId=ALLOWANCE), now=0)
    adapter = Allocator()
    adapter.fail = True
    worker = JobWorker(store, adapter)
    worker.tick(now=3600)
    run = jobs.runs(store, job["id"])[0]
    assert run["status"] == "awaiting_budget" and not adapter.calls
    adapter.fail = False
    worker.tick(now=3601)
    assert adapter.allocations[0] == adapter.allocations[1]
    assert adapter.allocations[0][1]["action_id"] == run["id"]
    assert adapter.calls[0]["spending_job_id"] == BUDGET
    worker.tick(now=3602)
    assert len(adapter.calls) == 1


def test_held_jobs_do_not_starve_ready_or_other_allowances(store):
    for index in range(22):
        jobs.create(
            store,
            definition(name=f"Held {index}", requireBudget=True, budgetAllowanceId=ALLOWANCE),
            now=0,
        )
    ready = jobs.create(store, definition(name="Ready"), now=0)
    adapter = Allocator()
    adapter.fail = True
    worker = JobWorker(store, adapter)
    worker.tick(now=3600)
    worker.tick(now=3601)
    assert len({item[1]["action_id"] for item in adapter.allocations}) == 22
    assert jobs.runs(store, ready["id"])[0]["status"] == "awaiting_approval"


def test_allowance_requires_budget_and_failure_is_redacted(store):
    with pytest.raises(ValueError):
        definition(budgetAllowanceId=ALLOWANCE)
    job = jobs.create(store, definition(requireBudget=True, budgetAllowanceId=ALLOWANCE), now=0)
    jobs.claim_due(store, now=3600)
    run = jobs.runs(store, job["id"])[0]
    adapter = Allocator()
    adapter.fail = True
    with pytest.raises(jobs.JobError) as error:
        jobs.provision_budget(store, run["id"], adapter)
    assert "private" not in str(error.value)


def test_executor_allocation_wire_contract(monkeypatch):
    import httpx
    from pi.job_execution import PublishedJobs
    from pi.toolgate import ToolGateClient

    seen = []

    def request(self, client, method, path, **kwargs):
        seen.append((method, path, kwargs["json"]))
        return httpx.Response(200, json={"job_id": BUDGET, "root_action_id": "run", "cap": 100})

    monkeypatch.setattr(PublishedJobs, "_request", request)
    adapter = PublishedJobs({"companion": ToolGateClient("http://gate", "scoped")})
    target = definition().target.model_dump()
    assert (
        adapter.allocate_budget(ALLOWANCE, target=target, action_id="run", agent_id="companion")
        == BUDGET
    )
    assert seen == [
        (
            "POST",
            f"/v2/agent/spending/allowances/{ALLOWANCE}/allocate",
            {"root_action_id": "run", "target": target},
        )
    ]
