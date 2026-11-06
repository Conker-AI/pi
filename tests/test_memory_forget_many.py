"""Forgetting several memories: one at a time, stop at the first refusal, replay on retry."""

import json
from contextlib import closing

import httpx
import pytest
from pydantic import ValidationError

from pi.conversation.store import Store
from pi.memory import forget as memory_forget
from pi.memory.corrections import Client

KEY = "synthetic-correction-key-for-tests-12345"
BATCH = "forget_batch_00001"


def body(*ids):
    return memory_forget.ForgetMany(
        request_id=BATCH,
        items=[memory_forget.ForgetItem(memory_id=i, expected_revision=1) for i in ids],
    )


class Gate:
    """MemoryGate's correction route: forgets once per request id, refuses the listed memories."""

    def __init__(self, refuse=(), status=409):
        self.calls, self.refuse, self.status = [], set(refuse), status

    def __call__(self, request):
        request_id = request.url.path.rsplit("/", 1)[1]
        payload = json.loads(request.content)
        self.calls.append((request_id, payload["memory_id"]))
        if payload["memory_id"] in self.refuse:
            return httpx.Response(self.status, json={"detail": "private"})
        return httpx.Response(
            200,
            json={
                "request_id": request_id,
                "memory_id": payload["memory_id"],
                "agent_id": "owner",
                "revision": payload["expected_revision"],
                "status": "forgotten",
                "index_removal": "removed",
            },
        )


def client(gate):
    return Client("http://memorygate", KEY, "owner", transport=httpx.MockTransport(gate))


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_every_item_is_its_own_forget_request_in_order(store):
    gate = Gate()
    result = memory_forget.forget_many(store, client(gate), body("mem-1", "mem-2", "mem-3"))
    assert gate.calls == [(f"{BATCH}-0", "mem-1"), (f"{BATCH}-1", "mem-2"), (f"{BATCH}-2", "mem-3")]
    assert result["stopped"] is None
    assert [r["memoryId"] for r in result["results"]] == ["mem-1", "mem-2", "mem-3"]
    assert {r["requestId"] for r in result["results"]} == {f"{BATCH}-{i}" for i in range(3)}


def test_a_later_refusal_stops_and_reports_what_was_forgotten(store):
    gate = Gate(refuse={"mem-2"})
    result = memory_forget.forget_many(store, client(gate), body("mem-1", "mem-2", "mem-3"))
    assert [r["memoryId"] for r in result["results"]] == ["mem-1"]
    assert result["stopped"] == {
        "index": 1,
        "memoryId": "mem-2",
        "code": "revision_conflict",
        "message": "The memory changed; review it again before forgetting.",
    }
    assert "mem-3" not in [memory for _, memory in gate.calls]
    assert "private" not in json.dumps(result)


def test_a_first_refusal_is_raised_as_it_is(store):
    with pytest.raises(memory_forget.ForgetError) as error:
        memory_forget.forget_many(store, client(Gate(refuse={"mem-1"}, status=404)), body("mem-1"))
    assert (error.value.detail["code"], error.value.status) == ("not_found", 404)


def test_repeating_the_batch_replays_done_items_and_goes_on(store):
    first = Gate(refuse={"mem-2"}, status=500)
    result = memory_forget.forget_many(store, client(first), body("mem-1", "mem-2"))
    assert result["stopped"]["code"] == "outcome_unknown"
    second = Gate()
    result = memory_forget.forget_many(store, client(second), body("mem-1", "mem-2"))
    # mem-1 is answered from Pi's own receipt; only the unknown one goes to MemoryGate again.
    assert second.calls == [(f"{BATCH}-1", "mem-2")]
    assert [r["memoryId"] for r in result["results"]] == ["mem-1", "mem-2"]
    assert result["stopped"] is None


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"memory_id": "mem-1", "expected_revision": 1}] * 2,
        [{"memory_id": f"mem-{i}", "expected_revision": 1} for i in range(51)],
    ],
)
def test_empty_duplicate_or_oversized_batches_are_refused(items):
    with pytest.raises(ValidationError):
        memory_forget.ForgetMany.model_validate({"request_id": BATCH, "items": items})


def test_a_batch_request_id_leaves_room_for_the_item_suffix():
    with pytest.raises(ValidationError):
        memory_forget.ForgetMany.model_validate(
            {"request_id": "x" * 121, "items": [{"memory_id": "m", "expected_revision": 1}]}
        )
    longest = memory_forget.ForgetMany.model_validate(
        {"request_id": "x" * 120, "items": [{"memory_id": "m", "expected_revision": 1}]}
    )
    memory_forget.Forget(request_id=f"{longest.request_id}-49", memory_id="m", expected_revision=1)
