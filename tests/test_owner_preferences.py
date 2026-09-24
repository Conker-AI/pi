"""Durable configuration and pure policy checks, without dispatch or browser wiring."""

import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi import api, owner_preferences as prefs
from pi.store import Store


def settings(**changes):
    return prefs.OwnerPreferences.model_validate({**copy.deepcopy(prefs.DEFAULT), **changes})


def test_durability_and_concurrent_revision_conflict(tmp_path):
    path = tmp_path / "test.db"
    with closing(Store(path)) as store:
        assert prefs.load(store) == {"revision": 1, "preferences": prefs.DEFAULT}
        request = prefs.UpdatePreferences(expected_revision=1, preferences=settings(urgency="off"))

        def attempt(_):
            try:
                return prefs.save(store, request)
            except prefs.RevisionConflict as exc:
                return exc.current_revision

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        saved = next(result for result in results if isinstance(result, dict))
        assert 2 in results
        assert store.list_sessions() == []
    with closing(Store(path)) as store:
        assert prefs.load(store) == saved
        assert saved["preferences"]["urgency"] == "off"
        prefs.load(store)["preferences"]["urgency"] = "meaningful"
        assert prefs.load(store) == saved


@pytest.mark.parametrize(
    "path,value",
    [
        (("idleTimeoutMinutes",), True),
        (("idleTimeoutMinutes",), 7),
        (("urgency",), "all"),
        (("unexpected",), True),
        (("quietHours", "enabled"), 1),
        (("quietHours", "start"), "24:00"),
        (("quietHours", "end"), "22:00"),
        (("quietHours", "timeZone"), "not/a-zone"),
        (("dailyBudget", "costCents"), -1),
        (("dailyBudget", "costCents"), 1.5),
        (("dailyBudget", "suggestions"), 101),
        (("dailyBudget", "researchMinutes"), 1441),
    ],
)
def test_strict_fields(path, value):
    data = copy.deepcopy(prefs.DEFAULT)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        prefs.OwnerPreferences.model_validate(data)


@pytest.mark.parametrize(
    "instant,expected",
    [
        ("2026-01-01T19:59:59+00:00", False),
        ("2026-01-01T20:00:00+00:00", True),
        ("2026-01-02T04:59:59+00:00", True),
        ("2026-01-02T05:00:00+00:00", False),
    ],
)
def test_overnight_boundaries(instant, expected):
    assert prefs.quiet_now(settings(), datetime.fromisoformat(instant)) is expected


def test_dst_repeat_skip_and_day_boundary():
    value = settings(
        quietHours={
            "enabled": True,
            "start": "01:00",
            "end": "03:00",
            "timeZone": "America/New_York",
            "urgentExceptions": False,
        }
    )
    for instant in (
        "2026-11-01T05:30:00+00:00",
        "2026-11-01T06:30:00+00:00",
        "2026-03-08T06:59:00+00:00",
    ):
        assert prefs.quiet_now(value, datetime.fromisoformat(instant))
    assert not prefs.quiet_now(value, datetime.fromisoformat("2026-03-08T07:00:00+00:00"))
    assert (
        prefs.local_day(value, datetime.fromisoformat("2026-01-02T02:00:00+00:00")) == "2026-01-01"
    )
    with pytest.raises(ValueError):
        prefs.quiet_now(value, datetime(2026, 1, 1))


def test_urgency_budget_and_stale_usage():
    instant = datetime.fromisoformat("2026-01-01T22:00:00+00:00")
    zero = prefs.Usage(suggestions=0, researchMinutes=0, costCents=0)
    value = settings(
        urgency="off", quietHours={**prefs.DEFAULT["quietHours"], "urgentExceptions": True}
    )
    kwargs = dict(
        urgent=True,
        usage_day="2026-01-02",
        used=zero,
        proposed=prefs.Usage(suggestions=1, researchMinutes=1, costCents=1),
    )
    assert prefs.admission_reasons(value, instant, **kwargs) == [
        "proactivity_off",
        "daily_budget_costCents",
    ]
    value = settings(
        urgency="urgent_only", dailyBudget=dict(suggestions=0, researchMinutes=0, costCents=0)
    )
    reasons = prefs.admission_reasons(value, instant, **{**kwargs, "urgent": False})
    assert reasons == [
        "urgent_only",
        "quiet_hours",
        "daily_budget_suggestions",
        "daily_budget_researchMinutes",
        "daily_budget_costCents",
    ]
    with pytest.raises(ValueError):
        prefs.admission_reasons(value, instant, **{**kwargs, "usage_day": "2026-01-01"})


def test_admin_only_http_and_failed_save_atomic(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "test.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "owner_admin_test_key", raising=False)
        monkeypatch.setattr(
            api.app.state,
            "gateway_key_hash",
            hashlib.sha256(b"runtime_test_key").hexdigest(),
            raising=False,
        )
        client = TestClient(api.app)
        route = "/owner/preferences"
        body = {"expected_revision": 1, "preferences": prefs.DEFAULT}
        for method in ("get", "post"):
            args = {"json": body} if method == "post" else {}
            assert getattr(client, method)(route, **args).status_code == 401
            assert (
                getattr(client, method)(
                    route, headers={"X-Pi-Gateway-Key": "runtime_test_key"}, **args
                ).status_code
                == 403
            )
        headers = {"X-Pi-Key": "owner_admin_test_key"}
        assert client.get(route, headers=headers).json()["revision"] == 1
        assert client.post(route, headers=headers, json=body).json()["revision"] == 2
        conflict = client.post(route, headers=headers, json=body)
        assert conflict.status_code == 409 and conflict.json()["detail"]["current_revision"] == 2
        assert (
            client.post(
                route, headers=headers, json={**body, "expected_revision": True}
            ).status_code
            == 422
        )
        assert prefs.load(store)["revision"] == 2


def test_daytime_window_disabled_and_accumulated_budget():
    value = settings(quietHours={**prefs.DEFAULT["quietHours"], "start": "10:00", "end": "11:00"})
    instant = datetime.fromisoformat("2026-01-01T08:00:00+00:00")
    assert prefs.quiet_now(value, instant)
    assert not prefs.quiet_now(value, datetime.fromisoformat("2026-01-01T09:00:00+00:00"))
    value = settings(quietHours={**prefs.DEFAULT["quietHours"], "enabled": False})
    kwargs = dict(
        urgent=False,
        usage_day="2026-01-01",
        used=prefs.Usage(suggestions=3, researchMinutes=29, costCents=0),
        proposed=prefs.Usage(suggestions=1, researchMinutes=1, costCents=0),
    )
    assert prefs.admission_reasons(value, instant, **kwargs) == []
    kwargs["used"] = prefs.Usage(suggestions=4, researchMinutes=30, costCents=0)
    assert prefs.admission_reasons(value, instant, **kwargs) == [
        "daily_budget_suggestions",
        "daily_budget_researchMinutes",
    ]
