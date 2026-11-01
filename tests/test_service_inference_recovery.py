"""Tests for recovering interrupted service model calls."""

import json
import time
from contextlib import closing

import pytest

from pi.conversation.store import Store
from pi.models import service_inference
from tests.test_service_inference import MEMORYGATE, digest


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_a_call_cut_off_by_a_restart_is_failed_not_pending(store):
    # Insert test rows: one pending, one completed, one failed
    now = time.time()
    with store._connect() as db:
        # Pending call that should be recovered
        db.execute(
            "INSERT INTO service_model_calls(id,service,role,request_id,provider,model,status,"
            "started_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                "pending-1",
                "memorygate",
                "memory-sorting",
                "memorygate:sort:001",
                "ollama",
                "qwen3:4b",
                "pending",
                now - 10,
            ),
        )
        # Completed call that should remain unchanged
        db.execute(
            "INSERT INTO service_model_calls(id,service,role,request_id,provider,model,status,"
            "started_at,ended_at,elapsed_ms,input_tokens,output_tokens,cost_usd,actual_model) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "completed-1",
                "memorygate",
                "memory-sorting",
                "memorygate:sort:002",
                "ollama",
                "qwen3:4b",
                "completed",
                now - 20,
                now - 15,
                5000,
                12,
                3,
                None,
                "qwen3:4b-actual",
            ),
        )
        # Failed call that should remain unchanged
        db.execute(
            "INSERT INTO service_model_calls(id,service,role,request_id,provider,model,status,"
            "failure_kind,started_at,ended_at,elapsed_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "failed-1",
                "memorygate",
                "memory-sorting",
                "memorygate:sort:003",
                "ollama",
                "qwen3:4b",
                "failed",
                "provider_error",
                now - 30,
                now - 25,
                3000,
            ),
        )
        db.commit()

    # Call recover_interrupted
    recovered = service_inference.recover_interrupted(store)
    assert recovered == 1

    # Verify the pending call was marked as failed with 'interrupted'
    with store._connect() as db:
        rows = {
            row["id"]: dict(row)
            for row in db.execute("SELECT * FROM service_model_calls").fetchall()
        }

    # Check pending call was recovered
    pending = rows["pending-1"]
    assert pending["status"] == "failed"
    assert pending["failure_kind"] == "interrupted"
    assert pending["ended_at"] is not None
    assert pending["ended_at"] > pending["started_at"]
    assert pending["elapsed_ms"] is None  # Not measured during recovery

    # Check completed call is unchanged
    completed = rows["completed-1"]
    assert completed["status"] == "completed"
    assert completed["failure_kind"] is None
    assert completed["ended_at"] == now - 15
    assert completed["elapsed_ms"] == 5000
    assert completed["input_tokens"] == 12
    assert completed["output_tokens"] == 3

    # Check failed call is unchanged
    failed = rows["failed-1"]
    assert failed["status"] == "failed"
    assert failed["failure_kind"] == "provider_error"
    assert failed["ended_at"] == now - 25
    assert failed["elapsed_ms"] == 3000

    # Verify usage counts the recovered call as failed
    usage = service_inference.usage(store)["lastDay"]
    memorygate_usage = next(
        u for u in usage if u["service"] == "memorygate" and u["role"] == "memory-sorting"
    )
    assert memorygate_usage["calls"] == 3
    assert memorygate_usage["failed"] == 2  # original failed + recovered pending


def test_startup_fails_calls_left_pending(monkeypatch, tmp_path):
    from tests.test_service_inference import start

    # Create a store with a pending call
    with closing(Store(tmp_path / "startup.db")) as startup_store:
        now = time.time()
        with startup_store._connect() as db:
            db.execute(
                "INSERT INTO service_model_calls(id,service,role,request_id,provider,model,status,"
                "started_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "pending-startup",
                    "memorygate",
                    "memory-sorting",
                    "memorygate:sort:startup",
                    "ollama",
                    "qwen3:4b",
                    "pending",
                    now - 5,
                ),
            )
            db.commit()
        startup_store.close()

    # Run startup which should recover the pending call
    start(monkeypatch, tmp_path, json.dumps({"memorygate": digest(MEMORYGATE)}))

    # Open the store again and verify the call was recovered
    with closing(Store(tmp_path / "startup.db")) as verify_store, verify_store._connect() as db:
        rows = db.execute("SELECT * FROM service_model_calls").fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["status"] == "failed"
        assert row["failure_kind"] == "interrupted"
        assert row["ended_at"] is not None
