"""Shared fixtures for the memory-edit tests: a synthetic MemoryGate, an isolated store and an unresolved edit."""

import json
from contextlib import closing

import httpx
import pytest

from pi.conversation.store import Store
from pi.memory import edits
from pi.memory.corrections import Client

KEY = "synthetic-correction-capability-" + "k" * 32


BODY = edits.Apply(
    request_id="correction_request_0001",
    memory_id="mem-1",
    expected_revision=3,
    text="Owner-reviewed replacement 🐿",
)


class Remote:
    def __init__(self):
        self.memory_id = "mem-1"
        self.text, self.revision = "Original retained fact", 3
        self.receipts, self.calls, self.applied = {}, [], 0
        self.lose = self.before_dispatch = False
        self.reject = None
        self.malformed = False
        self.indexing = "indexed"

    def handle(self, request):
        assert request.headers["X-MemoryGate-Correction-Key"] == KEY
        assert request.headers["X-Agent-Id"] == "companion"
        assert "X-MemoryGate-Key" not in request.headers
        self.calls.append((request.method, request.url.path))
        identity = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET" and "/memories/" in request.url.path:
            if identity != self.memory_id:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "id": identity,
                    "agent_id": "companion",
                    "revision": self.revision,
                    "text": self.text,
                    "source_type": "inferred",
                    "confidence": "low",
                },
            )
        if request.method == "GET":
            return httpx.Response(
                200 if identity in self.receipts else 404, json=self.receipts.get(identity, {})
            )
        if self.before_dispatch:
            raise httpx.ReadTimeout("PRIVATE_PROVIDER_ERROR")
        if self.reject:
            return httpx.Response(self.reject, json={"detail": "PRIVATE_PROVIDER_ERROR"})
        body = json.loads(request.content)
        assert set(body) == {"memory_id", "expected_revision", "text"}
        digest = edits.remote_digest("companion", edits.Apply(request_id=identity, **body))
        if identity in self.receipts:
            if self.receipts[identity]["payload_hash"] != digest:
                return httpx.Response(409)
            return httpx.Response(200, json=self.receipts[identity])
        if body["expected_revision"] != self.revision:
            return httpx.Response(409)
        self.text, self.revision = body["text"], self.revision + 1
        self.applied += 1
        self.receipts[identity] = {
            "request_id": identity,
            "memory_id": body["memory_id"],
            "agent_id": "companion",
            "previous_revision": self.revision - 1,
            "revision": self.revision,
            "status": "applied",
            "payload_hash": digest,
            "indexing": self.indexing,
        }
        if self.lose:
            raise httpx.ReadTimeout("PRIVATE_PROVIDER_ERROR")
        return httpx.Response(200, json={} if self.malformed else self.receipts[identity])


@pytest.fixture
def setup(tmp_path):
    remote = Remote()
    with (
        closing(Store(tmp_path / "pi.db")) as store,
        closing(
            Client(
                "http://memorygate", KEY, "companion", transport=httpx.MockTransport(remote.handle)
            )
        ) as client,
    ):
        yield store, client, remote


def unresolved(setup):
    store, client, remote = setup
    remote.lose = True
    result = edits.apply(store, client, BODY)
    assert result["status"] == "unknown"
    return result
