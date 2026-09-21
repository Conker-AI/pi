"""Real dispatcher execution with injected adapters; no external requests."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi import FastAPI, HTTPException, Header
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi import agents, model_evaluations as e, model_evaluations_api, model_roles
from pi.providers import Completion, ProviderUnavailable
from pi.store import Store


class Adapter:
    def __init__(self, response='{"modelId":"a"}', fail=False):
        self.response, self.fail, self.calls = response, fail, []
    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append((messages, model, timeout))
        if self.fail: raise ProviderUnavailable("Do not persist provider secrets")
        return Completion(text=self.response, model="actual-returned", provider="test",
            input_tokens=12, output_tokens=4, cached_tokens=None, cost_usd=0.004)


def configuration():
    role = dict(enabled=True, eligibleModelIds=["a", "b"], modelId="a", timeoutMs=1200,
                failure="stop", fallbackModelId=None)
    return model_roles.Configuration.model_validate({
        "providers": [{"id": "test", "name": "Test", "enabled": True}, {"id": "other", "name": "Other", "enabled": True}],
        "models": [{"id": "a", "providerId": "test", "name": "A", "route": "requested-a", "enabled": True},
                   {"id": "b", "providerId": "other", "name": "B", "route": "requested-b", "enabled": True}],
        "defaultModelId": "a", "roleSettings": {"answerMode": "manual", "roles": {name: dict(role) for name in model_roles.ROLES}}})


def case(role="routing"):
    return e.Case(name=role, role=role, prompt="The meeting is on Monday. Budget is $10.",
        candidateIds=[] if role == "summarization" else ["a", "b"],
        expectedIds=[] if role == "summarization" else ["a"],
        requiredFacts=["Monday", "$10"] if role == "summarization" else [])


def request(identity="evaluation_request_001", case_revision=1, config_revision=1):
    return e.Run(request_id=identity, expected_case_revision=case_revision, expected_configuration_revision=config_revision)


def setup(store):
    model_roles.save(store, model_roles.Update(expected_revision=0, configuration=configuration()))


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "test.db")) as value:
        setup(value)
        yield value


@pytest.mark.parametrize("role,response,score", [
    ("routing", '{"modelId":"a"}', 1), ("routing", '{"modelId":"b"}', 0),
    ("routing", '{"modelId":"outside"}', 0), ("routing", '{"modelId":"a","modelId":"b"}', 0),
    ("routing", '```json\n{"modelId":"a"}\n```', 0),
    ("context-selection", '{"messageIds":["a"]}', 1),
    ("context-selection", '{"messageIds":["a","a"]}', 0),
    ("context-selection", '{"messageIds":["a","b"]}', 0),
    ("summarization", 'MONDAY, with a $10 budget.', 1),
    ("summarization", 'The meeting is Monday.', 0.5),
])
def test_actual_dispatch_and_bounded_metrics(store, role, response, score):
    saved = e.save_case(store, case(role))
    adapter = Adapter(response)
    result = e.evaluate(store, saved["id"], request(), {"test": adapter})
    evidence = result["result"]
    assert result["state"] == "complete" and evidence["metric"]["score"] == score
    assert len(adapter.calls) == 1 and adapter.calls[0][1:] == ("requested-a", 1.2)
    # Expected labels/facts are not sent to the evaluated helper.
    assert "requiredFacts" not in adapter.calls[0][0][1].content
    assert "expectedIds" not in adapter.calls[0][0][1].content
    assert evidence["attempts"][0]["actualModel"] == "actual-returned"
    assert evidence["providerCalls"][0]["requestedModel"] == "requested-a"
    assert evidence["latencyMs"] >= 0 and evidence["providerCalls"][0]["latencyMs"] >= 0
    assert evidence["usage"] == {"input_tokens": 12, "output_tokens": 4, "cached_tokens": None, "cost_usd": 0.004}
    assert evidence["semanticCorrectnessVerified"] is False
    assert model_roles.load(store)["configuration"]["defaultModelId"] == "a"


def test_durable_snapshots_replay_and_immutable_terminal_result(tmp_path):
    path = tmp_path / "test.db"
    with closing(Store(path)) as store:
        setup(store)
        saved = e.save_case(store, case())
        adapter = Adapter()
        first = e.evaluate(store, saved["id"], request(), {"test": adapter})
        updated = case().model_copy(update={"prompt": "Changed task"})
        e.save_case(store, updated, saved["id"], 1)
        config = configuration()
        config.roleSettings.roles["routing"].modelId = "b"
        model_roles.save(store, model_roles.Update(expected_revision=1, configuration=config))
        replay = e.evaluate(store, saved["id"], request(), {"test": adapter})
        assert replay["replayed"] and replay["snapshot"] == first["snapshot"] and len(adapter.calls) == 1
        with pytest.raises(e.EvaluationError, match="already used"):
            e.evaluate(store, saved["id"], request(case_revision=2), {"test": adapter})
        with store._connect() as db:
            for sql in ("UPDATE model_evaluation_runs SET result='{}'", "DELETE FROM model_evaluation_runs",
                        "UPDATE model_evaluation_case_versions SET definition='{}'",
                        "INSERT OR REPLACE INTO model_evaluation_runs SELECT * FROM model_evaluation_runs"):
                with pytest.raises(sqlite3.IntegrityError): db.execute(sql)
    with closing(Store(path)) as store:
        assert e.get_run(store, request().request_id) == {k: v for k, v in first.items() if k != "replayed"}


def test_concurrent_duplicate_does_not_repeat_provider_call(store):
    saved = e.save_case(store, case())
    entered, release = threading.Event(), threading.Event()
    class Waiting(Adapter):
        def complete_bounded(self, *args, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().complete_bounded(*args, **kwargs)
    adapter = Waiting()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(e.evaluate, store, saved["id"], request(), {"test": adapter})
        assert entered.wait(3)
        duplicate = e.evaluate(store, saved["id"], request(), {"test": adapter})
        assert duplicate["replayed"] and duplicate["state"] == "running"
        release.set()
        assert future.result()["state"] == "complete"
    assert len(adapter.calls) == 1


def test_late_provider_completion_cannot_overwrite_interrupted_receipt(store):
    saved = e.save_case(store, case())
    entered, release = threading.Event(), threading.Event()
    class Waiting(Adapter):
        def complete_bounded(self, *args, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().complete_bounded(*args, **kwargs)
    adapter = Waiting()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(e.evaluate, store, saved["id"], request(), {"test": adapter})
        assert entered.wait(3)
        # Simulate an interrupted receipt while its old caller is still returning.
        assert e.recover_interrupted(store) == 1
        release.set()
        result = future.result()
    assert result["state"] == "interrupted"
    assert result["result"]["error"] == "outcome_unknown_after_restart"
    assert e.evaluate(store, saved["id"], request(), {"test": adapter})["replayed"]
    assert len(adapter.calls) == 1


def test_interrupted_call_reopen_never_repeats(tmp_path):
    path = tmp_path / "test.db"
    class Crash(BaseException): pass
    class Crashing(Adapter):
        def complete_bounded(self, *args, **kwargs): raise Crash()
    with closing(Store(path)) as store:
        setup(store)
        saved = e.save_case(store, case())
        with pytest.raises(Crash): e.evaluate(store, saved["id"], request(), {"test": Crashing()})
    with closing(Store(path)) as store:
        assert e.recover_interrupted(store) == 1
        adapter = Adapter()
        replay = e.evaluate(store, saved["id"], request(), {"test": adapter})
        assert replay["state"] == "interrupted" and replay["replayed"] and not adapter.calls
        assert replay["result"]["usage"]["cost_usd"] is None


def test_failure_and_fallback_have_actual_call_records_and_unknown_usage(store):
    saved = e.save_case(store, case())
    value = configuration()
    role = value.roleSettings.roles["routing"]
    role.failure, role.fallbackModelId = "fallback", "b"
    model_roles.save(store, model_roles.Update(expected_revision=1, configuration=value))
    first, second = Adapter(fail=True), Adapter()
    run = e.evaluate(store, saved["id"], request(config_revision=2), {"test": first, "other": second})
    assert [c["status"] for c in run["result"]["providerCalls"]] == ["failed", "completed"]
    assert run["result"]["usage"]["cost_usd"] is None
    assert run["result"]["providerCalls"][1]["usage"]["cost_usd"] == 0.004
    failed = e.evaluate(store, saved["id"], request("evaluation_failure_002", config_revision=2), {"test": first})
    assert failed["state"] == "failed" and "secrets" not in str(failed)
    count = len(first.calls)
    assert e.evaluate(store, saved["id"], request("evaluation_failure_002", config_revision=2), {"test": first})["replayed"]
    assert len(first.calls) == count


def test_case_validation_and_revision_archive_rules(store):
    for changes in ({"prompt": " "}, {"role": "answer"}, {"expectedIds": ["outside"]},
                    {"candidateIds": ["a", "a"]}, {"requiredFacts": ["fact"]}):
        with pytest.raises(ValidationError): e.Case.model_validate({**case().model_dump(), **changes})
    saved = e.save_case(store, case())
    with pytest.raises(e.EvaluationError, match="unique"): e.save_case(store, case())
    with pytest.raises(e.EvaluationError, match="changed"):
        e.save_case(store, case(), saved["id"], 2)
    archived = e.archive_case(store, saved["id"], agents.ArchiveAgent(expected_revision=1, archived=True))
    with pytest.raises(e.EvaluationError, match="Restore"):
        e.evaluate(store, saved["id"], request(case_revision=2), {})
    restored = e.archive_case(store, saved["id"], agents.ArchiveAgent(expected_revision=archived["revision"], archived=False))
    assert restored["revision"] == 3 and restored["archived_at"] is None


def test_oversize_output_is_recorded_without_unbounded_body(store):
    saved = e.save_case(store, case("summarization"))
    result = e.evaluate(store, saved["id"], request(), {"test": Adapter("x" * 32001)})["result"]
    assert result["output"] is None and result["metric"]["error"] == "invalid_or_oversize_output"
    assert result["usage"]["output_tokens"] == 4


def test_owner_only_router_and_read_paths_never_dispatch(store):
    app, adapter = FastAPI(), Adapter()
    def authorize(x_owner_key: str | None = Header(None)):
        if x_owner_key != "owner": raise HTTPException(401)
    app.include_router(model_evaluations_api.router(lambda: store, authorize, lambda: {"test": adapter}))
    client, headers = TestClient(app), {"X-Owner-Key": "owner"}
    base = "/model-evaluations"
    assert client.get(base + "/cases").status_code == 401
    saved = client.post(base + "/cases", headers=headers, json=case().model_dump()).json()
    assert client.get(base + "/cases", headers=headers).status_code == 200
    assert client.get(base + "/runs", headers=headers).status_code == 200
    assert not adapter.calls
    url = base + "/cases/" + saved["id"] + "/runs"
    assert client.post(url, json=request().model_dump()).status_code == 401
    assert client.post(url, headers=headers, json=request().model_dump()).status_code == 200
    assert client.post(url, headers=headers, json=request().model_dump()).json()["replayed"]
    assert len(adapter.calls) == 1
