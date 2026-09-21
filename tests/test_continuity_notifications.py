from contextlib import closing
from datetime import UTC, datetime

import pytest

from pi import agents, owner_preferences
from pi import continuity as c
from pi.store import Store
from tests.test_continuity import event

DAY = datetime(2026, 9, 21, 10, tzinfo=UTC)
NIGHT = datetime(2026, 9, 21, 21, tzinfo=UTC)


def test_urgent_exceptions_are_per_event_and_off_still_wins(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        event(store, status="outcome_unknown")
        event(store, status="complete")
        prefs = owner_preferences.load(store)
        prefs["preferences"]["urgency"] = "urgent_only"
        prefs["preferences"]["quietHours"]["urgentExceptions"] = True
        owner_preferences.save(
            store,
            owner_preferences.UpdatePreferences(
                expected_revision=prefs["revision"], preferences=prefs["preferences"]
            ),
        )
        feed = c.notifications(store, now=NIGHT)
        assert len(feed["items"]) == 1
        assert feed["items"][0]["urgency"] == {"urgent": True, "basis": "uncertain-external-effect"}
        assert feed["items"][0]["status"] == "outcome_unknown"
        prefs = owner_preferences.load(store)
        prefs["preferences"]["urgency"] = "off"
        owner_preferences.save(
            store,
            owner_preferences.UpdatePreferences(
                expected_revision=prefs["revision"], preferences=prefs["preferences"]
            ),
        )
        assert c.notifications(store, now=NIGHT)["items"] == []
        assert len(c.briefing(store, now=NIGHT)["items"]) == 2


def test_cancelled_work_is_not_attention_or_completed(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        event(store, status="cancelled")
        summary = c.briefing(store, now=DAY)["summary"]
        assert summary["attentionUpdates"] == summary["completedUpdates"] == 0
        assert summary["cancelledUpdates"] == 1


def test_delivery_is_repeatable_until_ack_but_does_not_mark_read(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        event(store)
        first = c.notifications(store, now=DAY)
        assert len(first["items"]) == 1
        assert c.notifications(store, now=DAY) == first
        identity = first["items"][0]["eventId"]
        c.delivered(store, c.Acknowledge(event_ids=[identity]))
        c.delivered(store, c.Acknowledge(event_ids=[identity]))
        assert c.notifications(store, now=DAY)["items"] == []
        assert len(c.briefing(store, now=DAY)["items"]) == 1
    with closing(Store(path)) as store:
        assert c.notifications(store, now=DAY)["items"] == []


def test_quiet_hours_do_not_consume_notifications(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        event(store)
        assert c.notifications(store, now=NIGHT)["items"] == []
        assert "quiet_hours" in c.notifications(store, now=NIGHT)["suppressionReasons"]
        assert len(c.notifications(store, now=DAY)["items"]) == 1


def test_resolved_status_supersedes_blocker_and_receives_new_delivery(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        _, turn = event(store, status="failed")
        old = c.notifications(store, now=DAY)["items"][0]
        c.delivered(store, c.Acknowledge(event_ids=[old["eventId"]]))
        store.finish_turn(turn, "complete", expected_status="failed")
        new = c.notifications(store, now=DAY)["items"]
        assert len(new) == 1 and new[0]["eventId"] != old["eventId"]
        assert not new[0]["needsAttention"]


def test_ack_is_atomic_and_pagination_can_pass_delivered_items(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        for _ in range(3):
            event(store)
        first = c.notifications(store, now=DAY, limit=2)
        ids = [item["eventId"] for item in first["items"]]
        with pytest.raises(agents.AgentError):
            c.delivered(store, c.Acknowledge(event_ids=[ids[0], "missing"]))
        assert len(c.notifications(store, now=DAY)["items"]) == 3
        c.delivered(store, c.Acknowledge(event_ids=ids))
        page = c.notifications(store, now=DAY, limit=2)
        assert page["items"] == [] and page["nextCursor"]
        assert len(c.notifications(store, now=DAY, before=page["nextCursor"])["items"]) == 1
