"""Forgetting one memory: MemoryGate outcome mapping and Pi's cached copies. No network."""

import json
from contextlib import closing

import httpx
import pytest

from pi import memory_forget, memory_store
from pi.memory_corrections import Client
from pi.store import Store

KEY = "synthetic-correction-key-for-tests-12345"
BODY = memory_forget.Forget(
    request_id="forget_request_0001", memory_id="mem-1", expected_revision=2
)


def client(handler):
    return Client("http://memorygate", KEY, "owner", transport=httpx.MockTransport(handler))


def forgotten(request):
    assert request.method == "PUT"
    assert request.url.path == "/runtime/corrections/forget/forget_request_0001"
    assert json.loads(request.content) == {"memory_id": "mem-1", "expected_revision": 2}
    assert request.headers["X-MemoryGate-Correction-Key"] == KEY
    return httpx.Response(
        200,
        json={
            "request_id": "forget_request_0001",
            "memory_id": "mem-1",
            "agent_id": "owner",
            "revision": 2,
            "status": "forgotten",
            "index_removal": "removed",
        },
    )


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def turn_with_package(store, memories):
    session = store.create_session()
    turn = store.start_turn(session)
    memory_store.save_context(store, turn, "delivered", {"memories": memories})
    return turn


def test_forget_clears_only_packages_that_quoted_the_memory(store):
    quoted = turn_with_package(
        store, [{"id": "mem-1", "text": "secret address"}, {"id": "mem-2", "text": "tea"}]
    )
    unrelated = turn_with_package(store, [{"id": "mem-2", "text": "tea"}])
    lookalike = turn_with_package(store, [{"id": "mem-10", "text": "mentions mem-1 in text only"}])
    result = memory_forget.forget(store, client(forgotten), BODY)
    assert result == {
        "requestId": "forget_request_0001",
        "memoryId": "mem-1",
        "status": "forgotten",
        "indexRemoval": "removed",
        "cachedPackagesCleared": 1,
        "sourceConversationKept": True,
    }
    assert memory_store.context(store, quoted) == {
        **memory_store.context(store, quoted),
        "status": "redacted",
        "package": None,
    }
    assert memory_store.context(store, unrelated)["package"]["memories"][0]["id"] == "mem-2"
    assert memory_store.context(store, lookalike)["package"] is not None


@pytest.mark.parametrize(
    "status,code,http_status",
    [
        (404, "not_found", 404),
        (409, "revision_conflict", 409),
        (401, "unavailable", 503),
        (500, "unavailable", 503),
    ],
)
def test_memorygate_refusals_are_mapped_and_clear_nothing(store, status, code, http_status):
    quoted = turn_with_package(store, [{"id": "mem-1", "text": "secret"}])
    with pytest.raises(memory_forget.ForgetError) as error:
        memory_forget.forget(
            store, client(lambda request: httpx.Response(status, json={"detail": "private"})), BODY
        )
    assert (error.value.detail["code"], error.value.status) == (code, http_status)
    assert "private" not in str(error.value.detail)
    assert memory_store.context(store, quoted)["package"] is not None


def test_lost_reply_is_unknown_and_safe_to_repeat(store):
    def timeout(request):
        raise httpx.ReadTimeout("private")

    with pytest.raises(memory_forget.ForgetError) as error:
        memory_forget.forget(store, client(timeout), BODY)
    assert error.value.detail["code"] == "outcome_unknown"
    assert memory_forget.forget(store, client(forgotten), BODY)["status"] == "forgotten"


def test_unexpected_receipt_or_missing_capability_is_refused(store):
    def wrong(request):
        return httpx.Response(
            200, json={"agent_id": "owner", "memory_id": "other", "status": "forgotten"}
        )

    with pytest.raises(memory_forget.ForgetError) as error:
        memory_forget.forget(store, client(wrong), BODY)
    assert error.value.status == 502
    with pytest.raises(memory_forget.ForgetError) as error:
        memory_forget.forget(store, None, BODY)
    assert error.value.detail["code"] == "not_configured"


def test_preview_shows_the_exact_current_version():
    def snapshot(request):
        assert request.url.path == "/runtime/corrections/memories/mem-1"
        return httpx.Response(
            200,
            json={
                "id": "mem-1",
                "agent_id": "owner",
                "revision": 3,
                "text": "Lives at 12 Secret St",
                "source_type": "stated",
                "confidence": "high",
            },
        )

    assert memory_forget.preview(client(snapshot), "mem-1") == {
        "memoryId": "mem-1",
        "revision": 3,
        "text": "Lives at 12 Secret St",
    }
    with pytest.raises(memory_forget.ForgetError) as error:
        memory_forget.preview(client(lambda request: httpx.Response(404)), "mem-1")
    assert error.value.status == 404
