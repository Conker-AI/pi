import httpx
import pytest

from pi.job_execution import PublishedJobs
from pi.toolgate import ToolGateClient


def target(kind="automation"):
    return dict(kind=kind, id="report", publishedVersion=3, digest="a" * 64, args={"n": 4})


def adapter():
    return PublishedJobs({"companion": ToolGateClient("http://toolgate", "scoped-test-key")})


def receipt(**updates):
    return {
        "status": "completed",
        "action_id": "run-1",
        "definition_version": 3,
        "publication_digest": "a" * 64,
        "code": "OK",
        **updates,
    }


@pytest.mark.parametrize(
    "kind,path", [("tool", "/v2/tools/report/invoke"), ("automation", "/v2/automations/report/run")]
)
def test_exact_published_dispatch(monkeypatch, kind, path):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json=receipt(result={"ok": True}))

    monkeypatch.setattr(httpx, "post", post)
    assert adapter()(target(kind), action_id="run-1", agent_id="companion")["status"] == "completed"
    url, request = calls[0]
    assert url == "http://toolgate" + path
    assert request["json"] == {
        "action_id": "run-1",
        "args": {"n": 4},
        "published_version": 3,
        "expected_publication_digest": "a" * 64,
    }
    assert request["headers"] == {"X-ToolGate-Execution-Key": "scoped-test-key"}
    assert "scoped-test-key" not in str(request["json"])


def test_unprovisioned_agent_never_uses_companion_key(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not dispatch"))
    assert adapter()(target(), action_id="run-1", agent_id="other")["status"] == "failed"


@pytest.mark.parametrize(
    "body",
    [
        receipt(action_id="wrong"),
        receipt(publication_digest="b" * 64),
        receipt(definition_version=4),
        receipt(status="dispatching"),
        {},
        [],
    ],
)
def test_unverified_receipts_hold_run(monkeypatch, body):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=body))
    assert (
        adapter()(target(), action_id="run-1", agent_id="companion")["status"] == "outcome_unknown"
    )


def test_approval_and_definite_refusal(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: httpx.Response(
            200, json={"code": "CONFIRMATION_REQUIRED", "request_id": "approval-1"}
        ),
    )
    assert (
        adapter()(target(), action_id="run-1", agent_id="companion")["request_id"] == "approval-1"
    )
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: httpx.Response(409, json={"detail": {"code": "PUBLICATION_MISMATCH"}}),
    )
    assert adapter()(target(), action_id="run-1", agent_id="companion")["status"] == "failed"


def test_reconciliation_is_read_only_and_missing_is_unknown(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no retry"))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(404))
    assert (
        adapter().reconcile(target(), action_id="run-1", agent_id="companion")["status"]
        == "outcome_unknown"
    )
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=receipt()))
    assert (
        adapter().reconcile(target(), action_id="run-1", agent_id="companion")["status"]
        == "completed"
    )


def test_resume_transports_exact_approval_reference(monkeypatch):
    seen = []

    def post(*args, **kwargs):
        seen.append(kwargs["json"])
        return httpx.Response(200, json=receipt())

    monkeypatch.setattr(httpx, "post", post)
    assert (
        adapter()(
            target(), action_id="run-1", agent_id="companion", approval_request_id="saved-approval"
        )["status"]
        == "completed"
    )
    assert seen[0]["approval_request_id"] == "saved-approval"
    assert seen[0]["action_id"] == "run-1"


def test_transport_failure_not_retried(monkeypatch):
    calls = []

    def timeout(*a, **kw):
        calls.append(kw)
        raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(httpx, "post", timeout)
    assert (
        adapter()(target(), action_id="run-1", agent_id="companion")["status"] == "outcome_unknown"
    )
    assert len(calls) == 1
