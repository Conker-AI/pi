"""Tests for model profiles computed from recorded outcomes."""

import time
from contextlib import closing

from fastapi.testclient import TestClient
from model_profiles_helpers import (
    create_judgement,
    create_response_family,
    create_turn,
    create_vote,
    setup_models,
)

from pi.conversation.store import Store


def test_no_configuration_returns_empty_list(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        from pi.models import profiles as model_profiles

        result = model_profiles.profiles(store)
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

        from pi.models import profiles as model_profiles

        result = model_profiles.profiles(store)

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

        # Create response family for judgement
        root_msg_id = create_response_family(store, sid, msg_a, msg_b)

        # Create judgement
        create_judgement(
            store,
            sid,
            root_msg_id,
            [
                {"message_id": msg_a, "score": 8, "reason": "Good"},
                {"message_id": msg_b, "score": 6, "reason": "Okay"},
            ],
            msg_a,
        )

        from pi.models import profiles as model_profiles

        result = model_profiles.profiles(store)

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

        # Create response family for votes
        root_msg_id = create_response_family(store, sid, msg_a, msg_b)

        # Create votes
        create_vote(store, sid, root_msg_id, msg_a, "up")
        create_vote(store, sid, root_msg_id, msg_b, "down")
        create_vote(store, sid, root_msg_id, msg_a, "winner")  # This should replace the up vote

        from pi.models import profiles as model_profiles

        result = model_profiles.profiles(store)

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

        from pi.models import profiles as model_profiles

        result = model_profiles.profiles(store)

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


def test_profiles_read_a_real_judgement(tmp_path):
    """A judgement stored by the real judge is counted, so the two shapes cannot drift apart."""
    from contextlib import closing

    from pi.conversation import judgements
    from pi.conversation.store import Store
    from pi.models import profiles as model_profiles
    from pi.models.routing import Router
    from pi.turns.loop import Loop
    from tests.test_judgements import JUDGE_JSON, create_family
    from tests.test_judgements import setup_models as setup_judge_models
    from tests.test_response_retries import Recording

    with closing(Store(tmp_path / "pi.db")) as store:
        setup_judge_models(store)
        adapters = {"one": Recording(JUDGE_JSON), "two": Recording()}
        loop = Loop(store, Router(providers=adapters))
        sid = store.create_session()
        root, _version_b, _version_c = create_family(store, loop, sid)
        result = judgements.judge(
            store, adapters, sid, root, judgements.Judge(request_id="judge_request_profile")
        )
        scored = {score["message_id"] for score in result["scores"]}
        models = {m["id"]: m for m in model_profiles.profiles(store)["models"]}
        assert models["b"]["judge"]["judged"] + models["c"]["judge"]["judged"] == len(scored) == 2
        assert models["b"]["judge"]["wins"] + models["c"]["judge"]["wins"] == 1
