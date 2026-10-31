"""Tests for blind judge and owner votes."""

import json
import random
import threading
from contextlib import closing
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from test_loop import Recorder, loop_with

from pi.conversation import judgements, versions
from pi.conversation.store import Store
from pi.models import roles as model_roles
from pi.schedule import tasks
from tests.test_model_roles import config
from tests.test_response_retries import Recording
from tests.test_response_versions import alternative


def setup_models(store):
    """Setup model roles with models a, b, c."""
    value = config()
    value["models"].append(
        {
            "id": "c",
            "providerId": "one",
            "name": "Model C",
            "route": "actual-c",
            "enabled": True,
            "routingDescription": "",
        }
    )
    value["roleSettings"]["roles"]["answer"]["eligibleModelIds"].append("c")
    value["roleSettings"]["roles"]["routing"] = {
        **value["roleSettings"]["roles"]["answer"],
        "modelId": "a",
    }
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(value)
        ),
    )


def create_family(store, loop, sid):
    """Create a response family with three versions from models a, b, c."""
    original = loop.run_turn(sid, "Test question")["message"]
    _, version_b = alternative(store, sid, original["id"], "Answer from model b")
    _, version_c = alternative(store, sid, original["id"], "Answer from model c")
    with store._connect() as db:
        for mid, route in [
            (original["id"], "actual-a"),
            (version_b["id"], "actual-b"),
            (version_c["id"], "actual-c"),
        ]:
            db.execute(
                "UPDATE turns SET model=? WHERE id=?",
                (
                    route,
                    db.execute(
                        "SELECT turn_id FROM turn_messages WHERE message_id=? AND purpose='final'",
                        (mid,),
                    ).fetchone()[0],
                ),
            )
        db.commit()
    return original["id"], version_b["id"], version_c["id"]


def create_simple_family(store, sid):
    """Create a response family with three versions using simple loop_with."""
    loop = loop_with(store, Recorder())
    original = loop.run_turn(sid, "Test question")["message"]
    _, version_b = alternative(store, sid, original["id"], "Answer from model b")
    _, version_c = alternative(store, sid, original["id"], "Answer from model c")
    return original["id"], version_b["id"], version_c["id"]


JUDGE_JSON = json.dumps(
    {
        "scores": {
            "A": {"score": 8, "reason": "Good answer"},
            "B": {"score": 7, "reason": "Decent answer"},
        },
        "winner": "A",
    }
)


def test_judge_with_routing_role_excludes_own_model(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(JUDGE_JSON), "two": Recording(), "three": Recording()}
        from pi.models.routing import Router
        from pi.turns.loop import Loop

        loop = Loop(store, Router(providers=adapters))
        sid = store.create_session()
        root, version_b, version_c = create_family(store, loop, sid)
        body = judgements.Judge(request_id="judge_request_001", rubric="Test rubric")
        result = judgements.judge(store, adapters, sid, root, body)
        assert result["replayed"] is False
        assert result["judgeModelId"] == "a"
        assert len(result["excluded"]) == 1
        assert root in result["excluded"]
        assert len(result["scores"]) == 2
        assert result["winner"] in [version_b, version_c]
        judge_call = adapters["one"].messages[-1][1].content
        assert "actual-a" not in judge_call
        assert "actual-b" not in judge_call
        assert "actual-c" not in judge_call
        assert "A" in judge_call and "B" in judge_call


def test_judge_replay_no_new_calls(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(JUDGE_JSON), "two": Recording()}
        from pi.models.routing import Router
        from pi.turns.loop import Loop

        loop = Loop(store, Router(providers=adapters))
        sid = store.create_session()
        root, _version_b, _version_c = create_family(store, loop, sid)
        body = judgements.Judge(request_id="judge_request_001", rubric="Test rubric")
        result1 = judgements.judge(store, adapters, sid, root, body)
        assert result1["replayed"] is False
        call_count = len(adapters["one"].calls)
        result2 = judgements.judge(store, adapters, sid, root, body)
        assert result2["replayed"] is True
        assert result2["request_id"] == result1["request_id"]
        assert len(adapters["one"].calls) == call_count
        body2 = judgements.Judge(request_id="judge_request_001", rubric="Different rubric")
        with pytest.raises(judgements.JudgeError) as exc:
            judgements.judge(store, adapters, sid, root, body2)
        assert exc.value.code == "request_conflict"


def test_judge_with_specific_model(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        # Model b uses provider "two", so put judge response there
        adapters = {"one": Recording(), "two": Recording(JUDGE_JSON)}
        from pi.models.routing import Router
        from pi.turns.loop import Loop

        loop = Loop(store, Router(providers=adapters))
        sid = store.create_session()
        root, version_b, _version_c = create_family(store, loop, sid)
        body = judgements.Judge(request_id="judge_request_002", model_id="b")
        result = judgements.judge(store, adapters, sid, root, body)
        assert result["judgeModelId"] == "b"
        assert version_b in result["excluded"]
        assert len(result["scores"]) == 2


def test_judge_too_few_answers(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        # Model b uses provider "two"
        adapters = {"one": Recording(), "two": Recording(JUDGE_JSON)}
        from pi.models.routing import Router
        from pi.turns.loop import Loop

        loop = Loop(store, Router(providers=adapters))
        sid = store.create_session()
        original = loop.run_turn(sid, "Test question")["message"]
        _, version_b = alternative(store, sid, original["id"], "Answer from model b")
        with store._connect() as db:
            for mid, route in [(original["id"], "actual-a"), (version_b["id"], "actual-b")]:
                db.execute(
                    "UPDATE turns SET model=? WHERE id=?",
                    (
                        route,
                        db.execute(
                            "SELECT turn_id FROM turn_messages WHERE message_id=? AND purpose='final'",
                            (mid,),
                        ).fetchone()[0],
                    ),
                )
            db.commit()
        body = judgements.Judge(request_id="judge_request_003", model_id="b")
        calls_before = len(adapters["one"].calls) + len(adapters["two"].calls)
        with pytest.raises(judgements.JudgeError) as exc:
            judgements.judge(store, adapters, sid, original["id"], body)
        assert exc.value.code == "too_few"
        calls_after = len(adapters["one"].calls) + len(adapters["two"].calls)
        assert calls_after == calls_before


def test_judge_helpers_off(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(JUDGE_JSON), "two": Recording()}
        sid = store.create_session()
        from pi.conversation import settings as session_settings

        session_settings.save(
            store,
            sid,
            session_settings.Update(
                expected_revision=0,
                settings={
                    "agentId": "companion",
                    "privacy": {"memoryDisabled": False, "harnessDisabled": True},
                },
            ),
        )
        body = judgements.Judge(request_id="judge_request_004")
        with pytest.raises(judgements.JudgeError) as exc:
            judgements.judge(store, adapters, sid, "any_root", body)
        assert exc.value.code == "helpers_off"
        assert len(adapters["one"].calls) == 0


def test_judge_unknown_family(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(JUDGE_JSON), "two": Recording()}
        sid = store.create_session()
        body = judgements.Judge(request_id="judge_request_005")
        with pytest.raises(tasks.TaskError):
            judgements.judge(store, adapters, sid, "unknown_root", body)


def test_judge_unknown_model(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(JUDGE_JSON), "two": Recording()}
        sid = store.create_session()
        body = judgements.Judge(request_id="judge_request_006", model_id="unknown")
        with pytest.raises(judgements.JudgeError) as exc:
            judgements.judge(store, adapters, sid, "any_root", body)
        assert exc.value.code == "unknown_model"


def test_judge_excluded_version(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(JUDGE_JSON), "two": Recording()}
        from pi.models.routing import Router
        from pi.turns.loop import Loop

        loop = Loop(store, Router(providers=adapters))
        sid = store.create_session()
        root, version_b, _version_c = create_family(store, loop, sid)
        # Select version_b first so we can set an exclude rule on it
        versions.select(
            store, sid, root, versions.Selection(message_id=version_b, expected_revision=1)
        )
        from test_context_controls import policy

        from pi.context import controls as context

        context.save(
            store, sid, context.Update(expected_revision=0, policy=policy({version_b: "exclude"}))
        )
        body = judgements.Judge(request_id="judge_request_007")
        calls_before = len(adapters["one"].calls) + len(adapters["two"].calls)
        with pytest.raises(judgements.JudgeError) as exc:
            judgements.judge(store, adapters, sid, root, body)
        assert exc.value.code == "unavailable"
        calls_after = len(adapters["one"].calls) + len(adapters["two"].calls)
        assert calls_after == calls_before


@pytest.mark.parametrize(
    "malformed_response",
    [
        "not json",
        '{"scores": {"A": {"score": 7}}}',
        '{"winner": "A"}',
        '{"scores": {"A": {"score": 7}}, "winner": "A", "extra": "field"}',
        '{"scores": {"A": {"score": 0, "reason": "test"}}, "winner": "A"}',
        '{"scores": {"A": {"score": 11, "reason": "test"}}, "winner": "A"}',
        '{"scores": {"A": {"score": true, "reason": "test"}}, "winner": "A"}',
        '{"scores": {"A": {"score": 7, "reason": ""}}, "winner": "A"}',
        '{"scores": {"A": {"score": 7, "reason": "x" * 301}}, "winner": "A"}',
        '{"scores": {"A": {"score": 7, "reason": "test"}, "B": {"score": 8, "reason": "test"}}, "winner": "A"}',
        '{"scores": {"A": {"score": 7, "reason": "test"}}, "winner": "B"}',
    ],
)
def test_judge_malformed_replies_fail_closed(tmp_path, malformed_response):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(malformed_response), "two": Recording()}
        from pi.models.routing import Router
        from pi.turns.loop import Loop

        loop = Loop(store, Router(providers=adapters))
        sid = store.create_session()
        root, _version_b, _version_c = create_family(store, loop, sid)
        body = judgements.Judge(request_id="judge_request_008")
        with pytest.raises(judgements.JudgeError) as exc:
            judgements.judge(store, adapters, sid, root, body)
        assert exc.value.code == "unavailable"
        with store._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM response_judgements WHERE request_id=?", (body.request_id,)
            ).fetchone()
            assert row[0] == 0


def test_judge_shuffle_is_real(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        with patch.object(random.SystemRandom, "shuffle", side_effect=lambda lst: lst.reverse()):
            resp = json.dumps(
                {
                    "scores": {
                        "A": {"score": 7, "reason": "first"},
                        "B": {"score": 8, "reason": "second"},
                    },
                    "winner": "B",
                }
            )
            adapters = {"one": Recording(resp), "two": Recording()}
            from pi.models.routing import Router
            from pi.turns.loop import Loop

            loop = Loop(store, Router(providers=adapters))
            sid = store.create_session()
            root, version_b, _version_c = create_family(store, loop, sid)
            body = judgements.Judge(request_id="judge_request_009")
            result = judgements.judge(store, adapters, sid, root, body)
            assert result["winner"] == version_b


def test_votes_up_down_clear_round_trip(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        root, version_b, _version_c = create_simple_family(store, sid)
        body = judgements.Vote(message_id=version_b, vote="up")
        result = judgements.vote(store, sid, root, body)
        assert result["votes"][version_b] == "up"
        body = judgements.Vote(message_id=version_b, vote="down")
        result = judgements.vote(store, sid, root, body)
        assert result["votes"][version_b] == "down"
        body = judgements.Vote(message_id=version_b, vote="clear")
        result = judgements.vote(store, sid, root, body)
        assert version_b not in result["votes"]


def test_votes_winner_turns_previous_to_up(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        root, version_b, version_c = create_simple_family(store, sid)
        body = judgements.Vote(message_id=version_b, vote="winner")
        result = judgements.vote(store, sid, root, body)
        assert result["votes"][version_b] == "winner"
        body = judgements.Vote(message_id=version_c, vote="winner")
        result = judgements.vote(store, sid, root, body)
        assert result["votes"][version_b] == "up"
        assert result["votes"][version_c] == "winner"


def test_votes_non_version_error(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        root, _version_b, _version_c = create_simple_family(store, sid)
        body = judgements.Vote(message_id="not_a_version", vote="up")
        with pytest.raises(tasks.TaskError) as exc:
            judgements.vote(store, sid, root, body)
        assert exc.value.detail["code"] == "not_a_version"


def test_votes_dont_change_selected_message(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        root, _version_b, version_c = create_simple_family(store, sid)
        with store._connect() as db:
            family = versions._view(db, sid, root)
        initial_selected = family["selected_message_id"]
        body = judgements.Vote(message_id=version_c, vote="winner")
        judgements.vote(store, sid, root, body)
        with store._connect() as db:
            family = versions._view(db, sid, root)
        assert family["selected_message_id"] == initial_selected


def test_http_auth_requires_admin_key(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        from pi.api import server as api
        from pi.models.routing import Router
        from pi.turns.loop import Loop

        loop = Loop(store, Router())
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "router", loop.router, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "admin_key", raising=False)
        client = TestClient(api.app)
        url = "/sessions/test_session/response-families/test_root/judge"
        response = client.post(url, json={"request_id": "judge_request_000", "rubric": ""})
        assert response.status_code == 401
        response = client.post(
            url,
            json={"request_id": "judge_request_000", "rubric": ""},
            headers={"X-Pi-Key": "admin_key"},
        )
        assert response.status_code in [404, 422, 200]


def test_judge_accepts_a_pinned_version(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(JUDGE_JSON), "two": Recording()}
        from pi.models.routing import Router
        from pi.turns.loop import Loop

        loop = Loop(store, Router(providers=adapters))
        sid = store.create_session()
        root, version_b, _version_c = create_family(store, loop, sid)
        versions.select(
            store, sid, root, versions.Selection(message_id=version_b, expected_revision=1)
        )
        from test_context_controls import policy

        from pi.context import controls as context

        context.save(
            store,
            sid,
            context.Update(expected_revision=0, policy=policy({version_b: "keep-exact"})),
        )
        result = judgements.judge(
            store, adapters, sid, root, judgements.Judge(request_id="judge_request_pinned")
        )
        assert version_b in {score["message_id"] for score in result["scores"]}


def test_vote_value_is_validated():
    with pytest.raises(ValueError):
        judgements.Vote(message_id="msg_1", vote="maybe")


def test_a_concurrent_retry_waits_for_the_first_judge_call_and_replays_it(tmp_path):
    """Two calls with one request id pay for one judge call; the later one replays the first."""
    entered, release = threading.Event(), threading.Event()

    def hold_the_first_call():
        if not entered.is_set():
            entered.set()
            release.wait(5)

    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(JUDGE_JSON), "two": Recording()}
        from pi.models.routing import Router
        from pi.turns.loop import Loop

        loop = Loop(store, Router(providers=adapters))
        sid = store.create_session()
        root, _version_b, _version_c = create_family(store, loop, sid)
        before = len(adapters["one"].messages)
        adapters["one"].callback = hold_the_first_call
        body = judgements.Judge(request_id="judge_request_concurrent", rubric="Test rubric")
        results = []

        def call():
            results.append(judgements.judge(store, adapters, sid, root, body))

        first = threading.Thread(target=call)
        first.start()
        assert entered.wait(5)
        second = threading.Thread(target=call)
        second.start()
        second.join(0.5)
        release.set()
        first.join(5)
        second.join(5)
        assert len(adapters["one"].messages) == before + 1
        assert sorted(result["replayed"] for result in results) == [False, True]
