"""Tests for model profiles computed from recorded outcomes."""

import json
import time
from contextlib import closing

from fastapi.testclient import TestClient

from pi.conversation.store import Store
from pi.models import roles as model_roles
from tests.test_model_roles import config


def setup_models(store):
    """Setup model roles with models a, b."""
    value = config()
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(value)
        ),
    )


def create_turn(
    store,
    session_id,
    model_route,
    status="complete",
    latency_ms=None,
    cost_usd=None,
    input_tokens=None,
    output_tokens=None,
):
    """Create a turn with specified metrics."""
    turn_id = f"trn_{time.time_ns() % (10**16):016x}"
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO turns (id, session_id, status, model, latency_ms, cost_usd, input_tokens, output_tokens, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                turn_id,
                session_id,
                status,
                model_route,
                latency_ms,
                cost_usd,
                input_tokens,
                output_tokens,
                time.time(),
                time.time(),
            ),
        )
        # Create a final message for this turn
        message_id = f"msg_{time.time_ns() % (10**16):016x}"
        db.execute(
            "INSERT INTO messages (id, session_id, seq, role, content, created_at) "
            "VALUES (?, ?, 1, 'assistant', ?, ?)",
            (message_id, session_id, json.dumps({"content": "test"}), time.time()),
        )
        db.execute(
            "INSERT INTO turn_messages (message_id, turn_id, purpose) VALUES (?, ?, 'final')",
            (message_id, turn_id),
        )
        db.commit()
    return turn_id


def create_judgement(store, session_id, root_message_id, scores, winner):
    """Create a judgement with scores."""
    request_id = f"req_{time.time_ns() % (10**16):016x}"
    result = {
        "request_id": request_id,
        "judgeModelId": "judge-model",
        "rubric": "",
        "scores": scores,
        "winner": winner,
        "excluded": [],
        "created_at": time.time(),
    }
    with store._connect() as db:
        db.execute(
            "INSERT INTO response_judgements (request_id, session_id, root_message_id, payload_hash, judge_model_id, result, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                session_id,
                root_message_id,
                "hash",
                "judge-model",
                json.dumps(result),
                time.time(),
            ),
        )
        db.commit()
    return request_id


def create_vote(store, session_id, root_message_id, message_id, vote):
    """Create an owner vote."""
    with store._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO response_votes (message_id, session_id, root_message_id, vote, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, session_id, root_message_id, vote, time.time()),
        )
        db.commit()


def test_no_configuration_returns_empty_list(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        from pi.models.profiles import profiles

        result = profiles.profiles(store)
        assert result == {"configurationRevision": 0, "models": []}


def test_counts_and_metrics_are_exact(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)

        # Create sessions
        sid1 = store.create_session()
        sid2 = store.create_session()

        # Create turns for model actual-a
        create_turn(
            store,
            sid1,
            "actual-a",
            "complete",
            latency_ms=100,
            cost_usd=0.01,
            input_tokens=10,
            output_tokens=20,
        )
        create_turn(
            store,
            sid1,
            "actual-a",
            "complete",
            latency_ms=200,
            cost_usd=0.02,
            input_tokens=15,
            output_tokens=25,
        )
        create_turn(
            store, sid2, "actual-a", "failed", latency_ms=300, cost_usd=0.03
        )  # Should not count

        # Create turns for model actual-b
        create_turn(
            store,
            sid1,
            "actual-b",
            "complete",
            latency_ms=150,
            cost_usd=0.015,
            input_tokens=12,
            output_tokens=22,
        )

        from pi.models.profiles import profiles

        result = profiles.profiles(store)

        models = {m["id"]: m for m in result["models"]}

        # Model a (actual-a)
        model_a = models["a"]
        assert model_a["answers"] == 2  # Only complete turns count
        assert model_a["latencyMs"]["median"] == 100  # median of [100, 200]
        assert model_a["latencyMs"]["p90"] == 200  # p90 of [100, 200]
        assert model_a["costUsd"]["total"] == 0.03
        assert model_a["costUsd"]["perAnswer"] == 0.015  # 0.03 / 2
        assert model_a["tokens"]["input"] == 25  # 10 + 15
        assert model_a["tokens"]["output"] == 45  # 20 + 25

        # Model b (actual-b)
        model_b = models["b"]
        assert model_b["answers"] == 1
        assert model_b["latencyMs"]["median"] == 150
        assert model_b["latencyMs"]["p90"] == 150
        assert model_b["costUsd"]["total"] == 0.015
        assert model_b["costUsd"]["perAnswer"] == 0.015
        assert model_b["tokens"]["input"] == 12
        assert model_b["tokens"]["output"] == 22


def test_judge_scores_and_wins_map_to_correct_models(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)

        sid = store.create_session()

        # Create turns and messages
        turn_a = create_turn(store, sid, "actual-a", "complete")
        turn_b = create_turn(store, sid, "actual-b", "complete")

        # Get message IDs
        with store._connect() as db:
            msg_a = db.execute(
                "SELECT message_id FROM turn_messages WHERE turn_id=? AND purpose='final'",
                (turn_a,),
            ).fetchone()[0]
            msg_b = db.execute(
                "SELECT message_id FROM turn_messages WHERE turn_id=? AND purpose='final'",
                (turn_b,),
            ).fetchone()[0]

        # Create judgement
        create_judgement(
            store,
            sid,
            "root",
            {msg_a: {"score": 8, "reason": "Good"}, msg_b: {"score": 6, "reason": "Okay"}},
            msg_a,
        )

        from pi.models.profiles import profiles

        result = profiles.profiles(store)

        models = {m["id"]: m for m in result["models"]}

        # Model a should have judge stats
        model_a = models["a"]
        assert model_a["judge"]["judged"] == 1
        assert model_a["judge"]["averageScore"] == 8.0
        assert model_a["judge"]["wins"] == 1

        # Model b should have judge stats
        model_b = models["b"]
        assert model_b["judge"]["judged"] == 1
        assert model_b["judge"]["averageScore"] == 6.0
        assert model_b["judge"]["wins"] == 0


def test_owner_votes_count_correctly(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)

        sid = store.create_session()

        # Create turns and messages
        turn_a = create_turn(store, sid, "actual-a", "complete")
        turn_b = create_turn(store, sid, "actual-b", "complete")

        # Get message IDs
        with store._connect() as db:
            msg_a = db.execute(
                "SELECT message_id FROM turn_messages WHERE turn_id=? AND purpose='final'",
                (turn_a,),
            ).fetchone()[0]
            msg_b = db.execute(
                "SELECT message_id FROM turn_messages WHERE turn_id=? AND purpose='final'",
                (turn_b,),
            ).fetchone()[0]

        # Create votes
        create_vote(store, sid, "root", msg_a, "up")
        create_vote(store, sid, "root", msg_b, "down")
        create_vote(store, sid, "root", msg_a, "winner")  # This should replace the up vote

        from pi.models.profiles import profiles

        result = profiles.profiles(store)

        models = {m["id"]: m for m in result["models"]}

        # Model a should have winner vote
        model_a = models["a"]
        assert model_a["votes"]["up"] == 0  # Replaced by winner
        assert model_a["votes"]["down"] == 0
        assert model_a["votes"]["winner"] == 1

        # Model b should have down vote
        model_b = models["b"]
        assert model_b["votes"]["up"] == 0
        assert model_b["votes"]["down"] == 1
        assert model_b["votes"]["winner"] == 0


def test_forgotten_chats_do_not_count(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)

        # Create sessions
        sid_normal = store.create_session()
        sid_forgotten = store.create_session()

        # Create turns in both sessions
        create_turn(store, sid_normal, "actual-a", "complete", latency_ms=100, cost_usd=0.01)
        create_turn(store, sid_forgotten, "actual-a", "complete", latency_ms=200, cost_usd=0.02)

        # Mark one session as forgotten
        with store._connect() as db:
            db.execute(
                "INSERT INTO forgetting_receipts (id, root_session_id, forgotten_at, confirmation, message_count, turn_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("receipt1", sid_forgotten, time.time(), "confirmation", 1, 1),
            )
            db.execute(
                "INSERT INTO forgotten_sessions (session_id, receipt_id) VALUES (?, ?)",
                (sid_forgotten, "receipt1"),
            )
            db.commit()

        from pi.models.profiles import profiles

        result = profiles.profiles(store)

        models = {m["id"]: m for m in result["models"]}
        model_a = models["a"]

        # Only the non-forgotten turn should count
        assert model_a["answers"] == 1
        assert model_a["latencyMs"]["median"] == 100
        assert model_a["costUsd"]["total"] == 0.01


def test_http_admin_key_works_owner_key_and_no_key_401(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)

        from pi.api.server import app

        # Setup app state
        app.state.store = store
        app.state.admin_key = "test-admin-key"

        client = TestClient(app)

        # Test with admin key - should work
        response = client.get("/models/profiles", headers={"X-Pi-Key": "test-admin-key"})
        assert response.status_code == 200
        assert "configurationRevision" in response.json()

        # Test with owner key - should 401
        response = client.get("/models/profiles", headers={"X-Pi-Owner-Key": "test-owner-key"})
        assert response.status_code == 401

        # Test with no key - should 401
        response = client.get("/models/profiles")
        assert response.status_code == 401
