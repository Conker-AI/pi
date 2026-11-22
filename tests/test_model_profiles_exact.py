"""Model profiles that stay exact with mixed judgements and votes, and what they leave out."""

import time
from contextlib import closing, contextmanager

from model_profiles_helpers import (
    create_judgement,
    create_response_family,
    create_turn,
    create_vote,
    setup_models,
)

from pi.conversation.store import Store
from pi.models import roles as model_roles


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
