from contextlib import closing
from datetime import datetime

import pytest

from pi import agents, forgetting, session_settings
from pi import continuity as c
from pi.store import Store


def event(store, title="Completed work", status="complete"):
    sid = store.create_session(title=title)
    turn = store.start_turn(sid)
    store.finish_turn(turn, status)
    return sid, turn


def test_actual_events_seen_state_and_restart(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid, turn = event(store)
        first = c.briefing(store)
        assert len(first["items"]) == 1
        item = first["items"][0]
        assert item["sessionId"] == sid and item["runId"] == turn
        assert item["provenance"] == "recorded-event" and not item["needsAttention"]
        assert first["summary"]["eventIds"] == [item["eventId"]]
        assert first["summary"]["highlights"][0]["runId"] == turn
        assert first["summary"]["completedUpdates"] == 1
        assert c.briefing(store)["items"] == first["items"]
        c.acknowledge(store, c.Acknowledge(event_ids=[item["eventId"]]))
    with closing(Store(path)) as store:
        assert c.briefing(store)["items"] == []
        assert c.briefing(store)["summary"]["highlights"] == []


def test_new_status_replaces_old_blocker_but_is_not_marked_seen(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        _, turn = event(store, status="failed")
        failed = c.briefing(store)["items"][0]
        assert failed["needsAttention"]
        c.acknowledge(store, c.Acknowledge(event_ids=[failed["eventId"]]))
        # Existing run status machinery generates the later event.
        assert store.finish_turn(turn, "complete", expected_status="failed")
        current = c.briefing(store)["items"]
        assert len(current) == 1 and current[0]["status"] == "complete"
        assert current[0]["eventId"] != failed["eventId"]


def test_paging_and_atomic_acknowledgement(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        for _ in range(3):
            event(store)
        first = c.briefing(store, limit=2)
        second = c.briefing(store, limit=2, before=first["nextCursor"])
        assert len(first["items"]) == 2 and len(second["items"]) == 1
        assert first["summary"]["scope"] == "returned-page"
        assert first["summary"]["hasMore"]
        assert first["summary"]["completedUpdates"] == 2
        assert not second["summary"]["hasMore"]
        with pytest.raises(agents.AgentError):
            c.acknowledge(store, c.Acknowledge(event_ids=[first["items"][0]["eventId"], "missing"]))
        assert len(c.briefing(store)["items"]) == 3


def test_private_and_forgotten_sources_do_not_surface(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        private = store.create_session(title="Private title")
        session_settings.save(
            store,
            private,
            session_settings.Update(
                expected_revision=0,
                settings=session_settings.Settings(
                    agentId="companion",
                    privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=False),
                ),
            ),
        )
        turn = store.start_turn(private)
        store.finish_turn(turn, "complete")
        sid, _ = event(store, title="Will forget")
        assert [i["title"] for i in c.briefing(store)["items"]] == ["Will forget"]
    forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])
    with closing(Store(path)) as store:
        assert c.briefing(store)["items"] == []
        assert c.briefing(store)["summary"]["eventIds"] == []


def test_quiet_hours_suppress_notifications_not_owner_inspection(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        event(store)
        night = c.briefing(store, now=datetime.fromisoformat("2026-09-21T23:00:00+03:00"))
        day = c.briefing(store, now=datetime.fromisoformat("2026-09-21T12:00:00+03:00"))
        assert night["suppressionReasons"] == ["quiet_hours"]
        assert not day["notificationSuppressed"]
        assert night["items"] == day["items"]
        assert day["notificationDelivery"] == "owner-poll; explicit delivery acknowledgement"
