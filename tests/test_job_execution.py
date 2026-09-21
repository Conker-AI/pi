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

    monkeypatch.setattr(
        PublishedJobs,
        "_request",
        lambda self, gate, method, path, **kw: post(
            gate.base_url + path, headers=gate._headers(), **kw
        ),
    )
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
    monkeypatch.setattr(PublishedJobs, "_request", lambda *a, **k: pytest.fail("must not dispatch"))
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
    monkeypatch.setattr(PublishedJobs, "_request", lambda *a, **k: httpx.Response(200, json=body))
    assert (
        adapter()(target(), action_id="run-1", agent_id="companion")["status"] == "outcome_unknown"
    )


def test_approval_and_definite_refusal(monkeypatch):
    monkeypatch.setattr(
        PublishedJobs,
        "_request",
        lambda *a, **k: httpx.Response(
            200, json={"code": "CONFIRMATION_REQUIRED", "request_id": "approval-1"}
        ),
    )
    assert (
        adapter()(target(), action_id="run-1", agent_id="companion")["request_id"] == "approval-1"
    )
    monkeypatch.setattr(
        PublishedJobs,
        "_request",
        lambda *a, **k: httpx.Response(409, json={"detail": {"code": "PUBLICATION_MISMATCH"}}),
    )
    assert adapter()(target(), action_id="run-1", agent_id="companion")["status"] == "failed"


def test_reconciliation_is_read_only_and_missing_is_unknown(monkeypatch):
    monkeypatch.setattr(PublishedJobs, "_request", lambda *a, **k: pytest.fail("no retry"))
    monkeypatch.setattr(PublishedJobs, "_request", lambda *a, **k: httpx.Response(404))
    assert (
        adapter().reconcile(target(), action_id="run-1", agent_id="companion")["status"]
        == "outcome_unknown"
    )
    monkeypatch.setattr(
        PublishedJobs, "_request", lambda *a, **k: httpx.Response(200, json=receipt())
    )
    assert (
        adapter().reconcile(target(), action_id="run-1", agent_id="companion")["status"]
        == "completed"
    )


def test_resume_transports_exact_approval_reference(monkeypatch):
    seen = []

    def post(*args, **kwargs):
        seen.append(kwargs["json"])
        return httpx.Response(200, json=receipt())

    monkeypatch.setattr(
        PublishedJobs,
        "_request",
        lambda self, gate, method, path, **kw: post(
            gate.base_url + path, headers=gate._headers(), **kw
        ),
    )
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

    monkeypatch.setattr(PublishedJobs, "_request", timeout)
    assert (
        adapter()(target(), action_id="run-1", agent_id="companion")["status"] == "outcome_unknown"
    )
    assert len(calls) == 1


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


@pytest.mark.parametrize("reconcile", [False, True])
@pytest.mark.parametrize("kind", ["valid", "oversized", "encoded", "redirect", "timeout"])
def test_bounded_transport_never_replays(monkeypatch, reconcile, kind):
    import json

    seen = []
    stream = Chunks([json.dumps(receipt()).encode()])
    if kind == "oversized":
        stream = Chunks([b"x" * (256 * 1024), b"x"])
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    real_client = httpx.Client
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)

    def handle(request):
        seen.append(request)
        assert request.headers["X-ToolGate-Execution-Key"] == "scoped-test-key"
        assert request.headers["Accept-Encoding"] == "identity"
        if kind == "timeout":
            raise httpx.ReadTimeout("synthetic transport failure")
        headers = {"Content-Encoding": "gzip"} if kind == "encoded" else {}
        if kind == "redirect":
            headers["Location"] = "http://other-service/steal"
        return httpx.Response(307 if kind == "redirect" else 200, headers=headers, stream=stream)

    run = PublishedJobs(
        {"companion": ToolGateClient("http://toolgate", "scoped-test-key")},
        transport=httpx.MockTransport(handle),
    )
    call = run.reconcile if reconcile else run
    result = call(target(), action_id="run-1", agent_id="companion")
    assert result["status"] == ("completed" if kind == "valid" else "outcome_unknown")
    assert len(seen) == 1
    assert seen[0].method == ("GET" if reconcile else "POST")
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    if kind != "timeout":
        assert stream.closed
    if not reconcile:
        body = json.loads(seen[0].content)
        assert body["action_id"] == "run-1"
        assert body["expected_publication_digest"] == "a" * 64


def test_stream_deadline_is_unknown_and_closes(monkeypatch):
    import pi.job_execution as module

    clock = iter([0, 0, 1000])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    stream = Chunks([b"{", b"}"])
    run = PublishedJobs(
        {"companion": ToolGateClient("http://toolgate", "scoped-test-key")},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
    )
    assert run(target(), action_id="run-1", agent_id="companion")["status"] == "outcome_unknown"
    assert stream.closed
