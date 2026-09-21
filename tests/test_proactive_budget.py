from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime

import pytest

from pi import owner_preferences as prefs
from pi import proactive_budget as budget
from pi.store import Store

DAY = datetime(2026, 9, 21, 10, tzinfo=UTC)


def request(identity, **kwargs):
    return budget.Request(
        request_id=f"request_{identity:016d}",
        expected_revision=1,
        proposed=prefs.Usage(suggestions=1, researchMinutes=10, costCents=0),
        **kwargs,
    )


def test_concurrent_admission_and_restart_replay(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:

        def reserve(i):
            try:
                return budget.reserve(store, request(i), now=DAY)
            except budget.Denied:
                return None

        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(reserve, range(8)))
        assert sum(result is not None for result in results) == 3
        assert budget.status(store, now=DAY)["reserved"] == {
            "suggestions": 3,
            "researchMinutes": 30,
            "costCents": 0,
        }
        accepted = next(i for i, result in enumerate(results) if result)
    with closing(Store(path)) as store:
        assert budget.reserve(store, request(accepted), now=DAY)["replayed"]
        assert budget.status(store, now=DAY)["reserved"]["researchMinutes"] == 30


def test_quiet_off_revision_and_no_refunds(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        night = datetime(2026, 9, 21, 21, tzinfo=UTC)
        with pytest.raises(budget.Denied, match="not admitted"):
            budget.reserve(store, request(1), now=night)
        assert budget.status(store, now=DAY)["reserved"]["suggestions"] == 0
        budget.reserve(store, request(1), now=DAY)
        current = prefs.load(store)
        current["preferences"]["urgency"] = "off"
        prefs.save(
            store, prefs.UpdatePreferences(expected_revision=1, preferences=current["preferences"])
        )
        assert budget.reserve(store, request(1), now=DAY)["replayed"]
        with pytest.raises(budget.Denied) as error:
            budget.reserve(store, request(2), now=DAY)
        assert error.value.reasons == ["preferences_changed"]
        body = request(2).model_copy(update={"expected_revision": 2})
        with pytest.raises(budget.Denied) as error:
            budget.reserve(store, body, now=DAY)
        assert error.value.reasons == ["proactivity_off"]
        assert budget.status(store, now=DAY)["reserved"]["suggestions"] == 1


def test_timezone_change_preserves_same_day_usage_and_identity_conflicts(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        budget.reserve(store, request(1), now=DAY)
        conflict = request(1).model_copy(update={"urgent": True})
        with pytest.raises(budget.Denied) as error:
            budget.reserve(store, conflict, now=DAY)
        assert error.value.reasons == ["request_identity_conflict"]
        current = prefs.load(store)
        current["preferences"]["quietHours"]["timeZone"] = "Europe/London"
        prefs.save(
            store, prefs.UpdatePreferences(expected_revision=1, preferences=current["preferences"])
        )
        assert budget.status(store, now=DAY)["reserved"]["suggestions"] == 1
        tomorrow = datetime(2026, 9, 22, 10, tzinfo=UTC)
        assert budget.status(store, now=tomorrow)["reserved"]["suggestions"] == 0
