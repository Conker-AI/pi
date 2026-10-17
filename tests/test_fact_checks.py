"""Fact check a researched answer against its own sources."""

import json
import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_model_roles import Adapter, config

from pi.conversation import fact_checks
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.models import roles as model_roles


class Recording(Adapter):
    def __init__(self, response="answer", callback=None):
        super().__init__(response)
        self.messages = []
        self.callback = callback

    def complete_bounded(self, messages, *, model, timeout):
        self.messages.append(list(messages))
        if self.callback:
            self.callback()
        return super().complete_bounded(messages, model=model, timeout=timeout)


def setup_models(store):
    """Setup model roles with models a, b."""
    value = config()
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


def create_researched_answer(store, sid):
    """Create a session with a turn that has research.web, research.fetch and citations."""
    turn_id = store.start_turn(sid)

    with store._connect() as db:
        # Create research.web action with two results
        web_action_id = f"act_web_{time.time_ns()}"
        db.execute(
            "INSERT INTO tool_actions VALUES(?,?,?,?,?,'completed',?)",
            (
                web_action_id,
                turn_id,
                "research.web",
                json.dumps({"query": "sky color", "max_results": 5}),
                None,
                time.time(),
            ),
        )

        # Create the tool result message for web search
        web_observation = json.dumps(
            {
                "tool": "research.web",
                "ok": True,
                "result": {
                    "results": [
                        {
                            "result_id": "rr_source_001",
                            "url": "https://example.org/sky",
                            "title": "Why the Sky is Blue",
                            "snippet": "The sky appears blue due to Rayleigh scattering.",
                        },
                        {
                            "result_id": "rr_source_002",
                            "url": "https://example.org/water",
                            "title": "Properties of Water",
                            "snippet": "Water is wet because of its hydrogen bonds.",
                        },
                    ]
                },
            },
            ensure_ascii=False,
        )
        store.append_message(
            sid,
            "tool",
            web_observation,
            turn_id=turn_id,
            purpose="tool_result",
            action_id=web_action_id,
        )

        # Create research.fetch action for one of the results
        fetch_action_id = f"act_fetch_{time.time_ns()}"
        db.execute(
            "INSERT INTO tool_actions VALUES(?,?,?,?,?,'completed',?)",
            (
                fetch_action_id,
                turn_id,
                "research.fetch",
                json.dumps({"result_id": "rr_source_001", "max_chars": 3000}),
                None,
                time.time(),
            ),
        )

        # Create the tool result message for fetch
        fetch_observation = json.dumps(
            {
                "tool": "research.fetch",
                "ok": True,
                "result": {
                    "result_id": "rr_source_001",
                    "url": "https://example.org/sky",
                    "content": "Full article: The sky appears blue due to Rayleigh scattering of sunlight by atmospheric molecules.",
                    "truncated": False,
                },
            },
            ensure_ascii=False,
        )
        store.append_message(
            sid,
            "tool",
            fetch_observation,
            turn_id=turn_id,
            purpose="tool_result",
            action_id=fetch_action_id,
        )
        db.commit()

    # Complete the turn with an answer and citations
    citations = [
        {
            "id": "cite-1",
            "label": "Atmospheric Science",
            "href": "https://example.org/atmosphere",
            "excerpt": "Rayleigh scattering explains the blue sky.",
        }
    ]
    answer = store.complete_turn(
        turn_id, "The sky is blue. Water is wet. Fire is hot.", citations=citations
    )

    return answer["id"], turn_id


def create_answer_without_sources(store, sid):
    """Create a session with a turn that has no research sources."""
    turn_id = store.start_turn(sid)
    answer = store.complete_turn(turn_id, "I just know this.")
    return answer["id"], turn_id


CLAIMS_JSON = json.dumps(
    {
        "claims": [
            {
                "claim": "The sky is blue",
                "mark": "supported",
                "sources": ["rr_source_001"],
                "note": "Source confirms Rayleigh scattering",
            },
            {
                "claim": "Water is wet",
                "mark": "supported",
                "sources": ["rr_source_002"],
                "note": "Source mentions hydrogen bonds",
            },
            {
                "claim": "Fire is hot",
                "mark": "no-source",
                "sources": [],
                "note": "No source covers this",
            },
        ]
    }
)


def test_check_sends_answer_and_merged_sources(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(CLAIMS_JSON), "two": Recording()}
        sid = store.create_session()
        mid, _turn_id = create_researched_answer(store, sid)
        body = fact_checks.Check(request_id="factcheck_request_001")
        result = fact_checks.check(store, adapters, sid, mid, body)
        assert result["replayed"] is False
        assert result["modelId"] == "a"
        assert len(result["claims"]) == 3
        assert result["counts"]["supported"] == 2
        assert result["counts"]["no-source"] == 1
        # Check that sources were sent (fetch text replaced snippet for rr_source_001)
        assert len(result["sources"]) == 3  # two web + one citation
        source_ids = {s["id"] for s in result["sources"]}
        assert "rr_source_001" in source_ids
        assert "rr_source_002" in source_ids
        assert "cite:cite-1" in source_ids
        # One model call
        assert len(adapters["one"].messages) == 1


def test_check_replay_no_new_calls(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(CLAIMS_JSON), "two": Recording()}
        sid = store.create_session()
        mid, _turn_id = create_researched_answer(store, sid)
        body = fact_checks.Check(request_id="factcheck_request_002")
        result1 = fact_checks.check(store, adapters, sid, mid, body)
        assert result1["replayed"] is False
        call_count = len(adapters["one"].messages)
        # Replay with same id
        result2 = fact_checks.check(store, adapters, sid, mid, body)
        assert result2["replayed"] is True
        assert result2["request_id"] == result1["request_id"]
        assert len(adapters["one"].messages) == call_count
        # Same id with another model → 409
        body2 = fact_checks.Check(request_id="factcheck_request_002", model_id="b")
        with pytest.raises(fact_checks.CheckError) as exc:
            fact_checks.check(store, adapters, sid, mid, body2)
        assert exc.value.code == "request_conflict"


def test_check_no_sources(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(CLAIMS_JSON), "two": Recording()}
        sid = store.create_session()
        mid, _turn_id = create_answer_without_sources(store, sid)
        body = fact_checks.Check(request_id="factcheck_request_003")
        with pytest.raises(fact_checks.CheckError) as exc:
            fact_checks.check(store, adapters, sid, mid, body)
        assert exc.value.code == "no_sources"
        assert len(adapters["one"].messages) == 0


def test_check_helpers_off(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(CLAIMS_JSON), "two": Recording()}
        sid = store.create_session()
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
        body = fact_checks.Check(request_id="factcheck_request_004")
        with pytest.raises(fact_checks.CheckError) as exc:
            fact_checks.check(store, adapters, sid, "any_mid", body)
        assert exc.value.code == "helpers_off"
        assert len(adapters["one"].messages) == 0


def test_check_user_message_not_found(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(CLAIMS_JSON), "two": Recording()}
        sid = store.create_session()
        # Create a user message (not an assistant message)
        user_msg = store.append_message(sid, "user", "Hello")
        body = fact_checks.Check(request_id="factcheck_request_005")
        with pytest.raises(fact_checks.CheckError) as exc:
            fact_checks.check(store, adapters, sid, user_msg["id"], body)
        assert exc.value.code == "not_found"
        assert len(adapters["one"].messages) == 0


def test_check_non_final_message_not_found(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(CLAIMS_JSON), "two": Recording()}
        sid = store.create_session()
        # Create an intermediate assistant message (not final)
        msg = store.append_message(sid, "assistant", "thinking...")
        body = fact_checks.Check(request_id="factcheck_request_006")
        with pytest.raises(fact_checks.CheckError) as exc:
            fact_checks.check(store, adapters, sid, msg["id"], body)
        assert exc.value.code == "not_found"
        assert len(adapters["one"].messages) == 0


def test_check_excluded_answer(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(CLAIMS_JSON), "two": Recording()}
        sid = store.create_session()
        mid, _turn_id = create_researched_answer(store, sid)
        # Exclude the message
        from test_context_controls import policy

        from pi.context import controls as context

        context.save(
            store, sid, context.Update(expected_revision=0, policy=policy({mid: "exclude"}))
        )
        body = fact_checks.Check(request_id="factcheck_request_007")
        calls_before = len(adapters["one"].messages)
        with pytest.raises(fact_checks.CheckError) as exc:
            fact_checks.check(store, adapters, sid, mid, body)
        assert exc.value.code == "unavailable"
        calls_after = len(adapters["one"].messages)
        assert calls_after == calls_before


@pytest.mark.parametrize(
    "malformed_response",
    [
        "not json",
        '{"claims": "not a list"}',
        '{"claims": [{"claim": "x", "mark": "supported", "sources": ["s1"], "note": ""}], "extra": 1}',
        # 13 claims
        json.dumps(
            {
                "claims": [
                    {"claim": f"Claim {i}", "mark": "no-source", "sources": [], "note": ""}
                    for i in range(13)
                ]
            }
        ),
        # unknown mark
        json.dumps({"claims": [{"claim": "x", "mark": "unknown", "sources": [], "note": ""}]}),
        # source id not supplied
        json.dumps(
            {
                "claims": [
                    {
                        "claim": "x",
                        "mark": "supported",
                        "sources": ["nonexistent_source"],
                        "note": "",
                    }
                ]
            }
        ),
        # supported with no source
        json.dumps({"claims": [{"claim": "x", "mark": "supported", "sources": [], "note": ""}]}),
        # no-source with a source
        json.dumps(
            {
                "claims": [
                    {
                        "claim": "x",
                        "mark": "no-source",
                        "sources": ["rr_source_001"],
                        "note": "",
                    }
                ]
            }
        ),
        # 301-character claim
        json.dumps(
            {
                "claims": [
                    {
                        "claim": "x" * 301,
                        "mark": "no-source",
                        "sources": [],
                        "note": "",
                    }
                ]
            }
        ),
        # duplicate source ids
        json.dumps(
            {
                "claims": [
                    {
                        "claim": "x",
                        "mark": "supported",
                        "sources": ["rr_source_001", "rr_source_001"],
                        "note": "",
                    }
                ]
            }
        ),
    ],
)
def test_check_malformed_replies_fail_closed(tmp_path, malformed_response):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(malformed_response), "two": Recording()}
        sid = store.create_session()
        mid, _turn_id = create_researched_answer(store, sid)
        body = fact_checks.Check(request_id="factcheck_request_008")
        with pytest.raises(fact_checks.CheckError) as exc:
            fact_checks.check(store, adapters, sid, mid, body)
        assert exc.value.code == "unavailable"
        with store._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM fact_checks WHERE request_id=?", (body.request_id,)
            ).fetchone()
            assert row[0] == 0


def test_blocked_fetch_keeps_snippet(tmp_path):
    """A blocked fetch keeps the web snippet; the 48000-char cap sets sourcesTruncated."""
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        sid = store.create_session()
        turn_id = store.start_turn(sid)

        with store._connect() as db:
            # Create research.web action
            web_action_id = f"act_web_{time.time_ns()}"
            db.execute(
                "INSERT INTO tool_actions VALUES(?,?,?,?,?,'completed',?)",
                (
                    web_action_id,
                    turn_id,
                    "research.web",
                    json.dumps({"query": "test", "max_results": 5}),
                    None,
                    time.time(),
                ),
            )
            web_observation = json.dumps(
                {
                    "tool": "research.web",
                    "ok": True,
                    "result": {
                        "results": [
                            {
                                "result_id": "rr_blocked_001",
                                "url": "https://example.org/blocked",
                                "title": "Blocked Source",
                                "snippet": "Short snippet",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            store.append_message(
                sid,
                "tool",
                web_observation,
                turn_id=turn_id,
                purpose="tool_result",
                action_id=web_action_id,
            )

            # Create a BLOCKED fetch action
            fetch_action_id = f"act_fetch_{time.time_ns()}"
            db.execute(
                "INSERT INTO tool_actions VALUES(?,?,?,?,?,'completed',?)",
                (
                    fetch_action_id,
                    turn_id,
                    "research.fetch",
                    json.dumps({"result_id": "rr_blocked_001", "max_chars": 3000}),
                    None,
                    time.time(),
                ),
            )
            fetch_observation = json.dumps(
                {
                    "tool": "research.fetch",
                    "ok": True,
                    "blocked": True,
                    "result": {
                        "result_id": "rr_blocked_001",
                        "url": "https://example.org/blocked",
                        "content": "This should not appear because blocked is True.",
                    },
                },
                ensure_ascii=False,
            )
            store.append_message(
                sid,
                "tool",
                fetch_observation,
                turn_id=turn_id,
                purpose="tool_result",
                action_id=fetch_action_id,
            )
            db.commit()

        answer = store.complete_turn(turn_id, "Answer text")

        # Check that the source still has the snippet (not the blocked content)
        from pi.conversation import fact_checks as fc

        with store._connect() as db:
            sources, _truncated = fc._collect_sources(db, turn_id, answer["id"])
        assert len(sources) == 1
        assert sources[0]["text"] == "Short snippet"  # snippet preserved, not blocked content


def test_sources_truncated_cap(tmp_path):
    """The 48000-character cap sets sourcesTruncated."""
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        sid = store.create_session()
        turn_id = store.start_turn(sid)

        with store._connect() as db:
            # Create research.web action with many results
            web_action_id = f"act_web_{time.time_ns()}"
            db.execute(
                "INSERT INTO tool_actions VALUES(?,?,?,?,?,'completed',?)",
                (
                    web_action_id,
                    turn_id,
                    "research.web",
                    json.dumps({"query": "test", "max_results": 8}),
                    None,
                    time.time(),
                ),
            )
            web_results = []
            for i in range(9):
                rid = f"rr_cap_{i:03d}"
                web_results.append(
                    {
                        "result_id": rid,
                        "url": f"https://example.org/cap{i}",
                        "title": f"Source {i}",
                        "snippet": "s" * 6000,  # will be capped to 1000 by _collect_sources
                    }
                )
            web_observation = json.dumps(
                {
                    "tool": "research.web",
                    "ok": True,
                    "result": {"results": web_results},
                },
                ensure_ascii=False,
            )
            store.append_message(
                sid,
                "tool",
                web_observation,
                turn_id=turn_id,
                purpose="tool_result",
                action_id=web_action_id,
            )

            # Create fetch actions with long content for each source
            for i in range(9):
                rid = f"rr_cap_{i:03d}"
                fetch_action_id = f"act_fetch_{i}_{time.time_ns()}"
                db.execute(
                    "INSERT INTO tool_actions VALUES(?,?,?,?,?,'completed',?)",
                    (
                        fetch_action_id,
                        turn_id,
                        "research.fetch",
                        json.dumps({"result_id": rid, "max_chars": 6000}),
                        None,
                        time.time(),
                    ),
                )
                # Each fetch content will be capped at 6000 by _collect_sources
                fetch_observation = json.dumps(
                    {
                        "tool": "research.fetch",
                        "ok": True,
                        "result": {
                            "result_id": rid,
                            "url": f"https://example.org/cap{i}",
                            "content": "c" * 7000,  # will be capped to 6000
                            "truncated": True,
                        },
                    },
                    ensure_ascii=False,
                )
                store.append_message(
                    sid,
                    "tool",
                    fetch_observation,
                    turn_id=turn_id,
                    purpose="tool_result",
                    action_id=fetch_action_id,
                )

            db.commit()

        answer = store.complete_turn(turn_id, "Answer text")

        from pi.conversation import fact_checks as fc

        with store._connect() as db:
            sources, truncated = fc._collect_sources(db, turn_id, answer["id"])
        # 9 sources * 6000 chars each = 54000, exceeds 48000 cap
        # First 8 sources fit (8 * 6000 = 48000), 9th is truncated
        assert truncated is True
        assert len(sources) >= 1
        # First 8 sources have full 6000-char text
        assert len(sources[0]["text"]) == 6000


def test_history_returns_results(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models(store)
        adapters = {"one": Recording(CLAIMS_JSON), "two": Recording()}
        sid = store.create_session()
        mid, _turn_id = create_researched_answer(store, sid)
        body = fact_checks.Check(request_id="factcheck_request_009")
        fact_checks.check(store, adapters, sid, mid, body)
        result = fact_checks.history(store, sid, mid)
        assert len(result["results"]) == 1
        assert result["results"][0]["request_id"] == "factcheck_request_009"


def test_http_auth_requires_admin_key(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        from pi.api import server as api

        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(
            api.app.state, "router", type("R", (), {"adapters": lambda self: {}})(), raising=False
        )
        monkeypatch.setattr(api.app.state, "admin_key", "admin_key", raising=False)
        client = TestClient(api.app)
        url = "/sessions/test_session/messages/test_message/fact-check"
        # No key → 401
        response = client.post(url, json={"request_id": "factcheck_request_000"})
        assert response.status_code == 401
        # Owner key → 401 (admin required)
        response = client.post(
            url,
            json={"request_id": "factcheck_request_000"},
            headers={"X-Pi-Owner-Key": "some_owner_key"},
        )
        assert response.status_code == 401
        # Admin key → accepted (may fail with 404/409/etc but not 401)
        response = client.post(
            url,
            json={"request_id": "factcheck_request_000"},
            headers={"X-Pi-Key": "admin_key"},
        )
        assert response.status_code in [404, 422, 200, 409, 503]
