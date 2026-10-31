"""Tests for model profiles computed from recorded outcomes."""

import json
import time
from contextlib import closing, contextmanager

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
    """Create a turn with specified metrics using proper Store API."""
    # Start the turn
    turn_id = store.start_turn(session_id)

    # Prepare fields for the turn
    turn_fields = {
        "model": model_route,
        "latency_ms": latency_ms,
        "cost_usd": cost_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    # Remove None values
    turn_fields = {k: v for k, v in turn_fields.items() if v is not None}

    if status == "complete":
        # Complete the turn with a test message and metrics
        store.complete_turn(turn_id, "test", **turn_fields)
    else:
        # For non-complete status, we need to use finish_turn directly with a message first
        # Create the message manually since we're not using complete_turn
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Create the message first
            from pi.conversation import submissions

            submissions.append(
                db, session_id, "assistant", "test", turn_id=turn_id, purpose="final"
            )
            # Then finish the turn with the desired status and metrics
            store._finish_turn(db, turn_id, status, **turn_fields)
            db.commit()

    return turn_id


def create_response_family(store, session_id, message_a_id, message_b_id):
    """Create a response family with two versions for testing judgements and votes."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        # Create response family with message_a as root
        root = message_a_id
        db.execute("INSERT INTO response_families VALUES(?,?,?,1)", (root, session_id, root))
        db.execute("INSERT INTO response_versions VALUES(?,?,NULL)", (root, root))
        # Add message_b as version of root
        db.execute(
            "INSERT INTO response_versions VALUES(?,?,?)", (message_b_id, root, message_a_id)
        )
        db.commit()
    return root


def create_judgement(store, session_id, root_message_id, scores, winner, request_id=None):
    """Create a judgement with scores."""
    if request_id is None:
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


def final_messages(store, *turn_ids):
    """Map each turn id to its final message id."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT turn_id, message_id FROM turn_messages WHERE purpose='final'"
        ).fetchall()
    messages = {row["turn_id"]: row["message_id"] for row in rows}
    return [messages[turn_id] for turn_id in turn_ids]


def test_profiles_result_is_exact_with_mixed_judgements_and_votes(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        sid1 = store.create_session()
        sid2 = store.create_session()

        specs = [
            (sid1, "actual-a", 100, 0.25, 10, 20),
            (sid1, "actual-b", 150, 0.25, 12, 22),
            (sid2, "actual-a", 120, 0.125, 11, 21),
            (sid2, "actual-b", 170, 0.5, 13, 23),
        ]
        turns = [
            create_turn(
                store,
                sid,
                route,
                "complete",
                latency_ms=lat,
                cost_usd=cost,
                input_tokens=n_in,
                output_tokens=n_out,
            )
            for sid, route, lat, cost, n_in, n_out in specs
        ]
        msg_a1, msg_b1, msg_a2, msg_b2 = final_messages(store, *turns)
        root1 = create_response_family(store, sid1, msg_a1, msg_b1)
        root2 = create_response_family(store, sid2, msg_a2, msg_b2)

        score_a1 = {"message_id": msg_a1, "score": 8, "reason": "Good"}
        score_b1 = {"message_id": msg_b1, "score": 6, "reason": "Okay"}
        score_a2 = {"message_id": msg_a2, "score": 7, "reason": "Fine"}
        score_b2 = {"message_id": msg_b2, "score": 9, "reason": "Great"}
        orphan = {"message_id": "msg-in-no-turn", "score": 5, "reason": "Orphan"}
        # Two judgements naming messages of both models plus one message that
        # belongs to no turn; each judgement has its own winner.
        create_judgement(store, sid1, root1, [score_a1, score_b1], msg_a1)
        create_judgement(store, sid2, root2, [score_a2, score_b2, orphan], msg_b2)

        # All three vote kinds plus one kind the profiles never count.
        create_vote(store, sid1, root1, msg_a1, "up")
        create_vote(store, sid1, root1, msg_b1, "down")
        create_vote(store, sid2, root2, msg_a2, "winner")
        with store._connect() as db:
            # The vote column carries a CHECK constraint; bypass it to store an
            # unknown kind like a malformed row would have.
            db.execute("PRAGMA ignore_check_constraints = ON")
            db.execute(
                "INSERT OR REPLACE INTO response_votes "
                "(message_id, session_id, root_message_id, vote, updated_at) VALUES (?, ?, ?, ?, ?)",
                (msg_b2, sid2, root2, "meh", time.time()),
            )

        from pi.models import profiles as model_profiles

        result = model_profiles.profiles(store)

        assert result == {
            "configurationRevision": model_roles.load(store)["revision"],
            "models": [
                {
                    "id": "a",
                    "name": "A",
                    "providerId": "one",
                    "enabled": True,
                    "answers": 2,
                    "latencyMs": {"median": 100, "p90": 120},
                    "costUsd": {"total": 0.375, "perAnswer": 0.1875},
                    "tokens": {"input": 21, "output": 41},
                    "judge": {"judged": 2, "averageScore": 7.5, "wins": 1},
                    "votes": {"up": 1, "down": 0, "winner": 1},
                },
                {
                    "id": "b",
                    "name": "B",
                    "providerId": "two",
                    "enabled": True,
                    "answers": 2,
                    "latencyMs": {"median": 150, "p90": 170},
                    "costUsd": {"total": 0.75, "perAnswer": 0.375},
                    "tokens": {"input": 25, "output": 45},
                    "judge": {"judged": 2, "averageScore": 7.5, "wins": 1},
                    "votes": {"up": 0, "down": 1, "winner": 0},
                },
            ],
        }


def test_query_count_does_not_grow_with_judgements_and_votes(tmp_path, monkeypatch):
    from pi.models import profiles as model_profiles

    def counted_statements(n_judgements, n_votes):
        with closing(Store(tmp_path / f"pi_{n_judgements}.db")) as store:
            setup_models(store)
            sid = store.create_session()
            turn_a = create_turn(store, sid, "actual-a", "complete")
            turn_b = create_turn(store, sid, "actual-b", "complete")
            msg_a, msg_b = final_messages(store, turn_a, turn_b)
            root = create_response_family(store, sid, msg_a, msg_b)
            score_a = {"message_id": msg_a, "score": 8, "reason": "Good"}
            score_b = {"message_id": msg_b, "score": 6, "reason": "Okay"}
            for i in range(n_judgements):
                create_judgement(store, sid, root, [score_a, score_b], msg_a, request_id=f"bulk{i}")
            for i in range(n_votes):
                target = msg_a if i % 2 == 0 else msg_b
                create_vote(store, sid, root, target, ("up", "down", "winner")[i % 3])

            statements: list[str] = []
            real_connect = store._connect

            @contextmanager
            def traced_connect():
                with real_connect() as db:
                    db.set_trace_callback(statements.append)
                    yield db

            monkeypatch.setattr(store, "_connect", traced_connect)
            model_profiles.profiles(store)
            monkeypatch.undo()
            return len(statements)

    one = counted_statements(1, 1)
    many = counted_statements(60, 60)
    assert one == many


def test_forgotten_session_judgements_and_votes_do_not_count(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        sid_n = store.create_session()
        sid_f = store.create_session()

        turn_n = create_turn(store, sid_n, "actual-a")
        turn_nb = create_turn(store, sid_n, "actual-b")
        turn_f = create_turn(store, sid_f, "actual-a")
        turn_fb = create_turn(store, sid_f, "actual-b")
        msg_n, msg_nb, msg_f, msg_fb = final_messages(store, turn_n, turn_nb, turn_f, turn_fb)
        root_n = create_response_family(store, sid_n, msg_n, msg_nb)
        root_f = create_response_family(store, sid_f, msg_f, msg_fb)

        score_n = {"message_id": msg_n, "score": 9, "reason": "Good"}
        score_nb = {"message_id": msg_nb, "score": 7, "reason": "Okay"}
        score_f = {"message_id": msg_f, "score": 5, "reason": "Forgotten"}
        score_fb = {"message_id": msg_fb, "score": 4, "reason": "Forgotten"}
        create_judgement(store, sid_n, root_n, [score_n, score_nb], msg_n)
        create_judgement(store, sid_f, root_f, [score_f, score_fb], msg_f)
        create_vote(store, sid_n, root_n, msg_n, "winner")
        create_vote(store, sid_f, root_f, msg_f, "up")

        with store._connect() as db:
            db.execute(
                "INSERT INTO forgetting_receipts (id, root_session_id, forgotten_at, confirmation, message_count, turn_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("receipt1", sid_f, time.time(), "confirmation", 2, 2),
            )
            db.execute(
                "INSERT INTO forgotten_sessions (session_id, receipt_id) VALUES (?, ?)",
                (sid_f, "receipt1"),
            )

        from pi.models import profiles as model_profiles

        result = model_profiles.profiles(store)

        models = {m["id"]: m for m in result["models"]}
        model_a = models["a"]
        assert model_a["answers"] == 1
        assert model_a["judge"] == {"judged": 1, "averageScore": 9.0, "wins": 1}
        assert model_a["votes"] == {"up": 0, "down": 0, "winner": 1}
        model_b = models["b"]
        assert model_b["judge"] == {"judged": 1, "averageScore": 7.0, "wins": 0}
        assert model_b["votes"] == {"up": 0, "down": 0, "winner": 0}
