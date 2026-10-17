"""Fact-check sources: blocked page reads and the source text cap."""

import json
import time
from contextlib import closing

from pi.conversation.store import Store
from tests.test_fact_checks import setup_models


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
                    "result": {
                        "blocked": True,
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
