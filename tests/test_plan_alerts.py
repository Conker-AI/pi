"""Plan usage alerts: raised once per window period at the owner's level, closed when it resets."""

import hashlib
import json
from contextlib import closing
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from pi.agents import inbox
from pi.api import server
from pi.conversation.store import Store
from pi.models.chatgpt import ChatGPTProvider
from pi.owner import plan_alerts, preferences

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC).timestamp()
FRIDAY = "2026-10-23T09:00:00Z"
NEXT_FRIDAY = "2026-10-30T09:00:00Z"


def plan(used, resets=FRIDAY, provider="chatgpt", available=True):
    return {
        "provider": provider,
        "available": available,
        "plan": "plus",
        "windows": [
            {
                "id": "secondary",
                "label": "weekly",
                "usedPercent": used,
                "resetsAt": resets,
                "leftPercent": 100 - used,
                "minutes": 10080,
            }
        ],
        "checkedAt": "2026-10-21T12:00:00Z",
    }


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_an_alert_is_raised_once_at_the_level(store):
    assert plan_alerts.record(store, plan(79), 80, now=NOW) == []
    raised = plan_alerts.record(store, plan(81), 80, now=NOW + 60)
    assert [change["change"] for change in raised] == ["raised"]
    assert plan_alerts.record(store, plan(93), 80, now=NOW + 120) == []
    [item] = plan_alerts.needs_you(store)
    assert item["title"] == "ChatGPT weekly limit 93% used"
    assert item["resetsAt"] == FRIDAY and item["since"] == NOW + 60
    assert plan_alerts.overview(store)["windows"][0]["usedPercent"] == 93


def test_the_window_resetting_closes_the_alert_and_a_new_period_can_raise_again(store):
    plan_alerts.record(store, plan(85), 80, now=NOW)
    friday = datetime(2026, 10, 23, 9, 30, tzinfo=UTC).timestamp()
    changes = plan_alerts.record(store, plan(3, NEXT_FRIDAY), 80, now=friday)
    assert [change["change"] for change in changes] == ["reset"]
    assert plan_alerts.needs_you(store) == []
    [reset] = plan_alerts.resets(store, friday - 1)
    assert reset["kind"] == "plan-reset" and reset["title"] == "ChatGPT weekly limit reset"
    assert [
        c["change"] for c in plan_alerts.record(store, plan(90, NEXT_FRIDAY), 80, now=friday + 9)
    ] == ["raised"]


def test_a_reset_time_that_jitters_is_the_same_period(store):
    plan_alerts.record(store, plan(85, "2026-10-23T09:00:00Z"), 80, now=NOW)
    assert plan_alerts.record(store, plan(86, "2026-10-23T09:00:30Z"), 80, now=NOW + 60) == []
    changes = plan_alerts.record(store, plan(20, "2026-10-30T09:00:00Z"), 80, now=NOW + 120)
    assert [change["change"] for change in changes] == ["reset"]


def test_a_dismissed_alert_stays_quiet_until_the_next_period(store):
    [raised] = plan_alerts.record(store, plan(85), 80, now=NOW)
    assert plan_alerts.dismiss(store, raised["alertId"], now=NOW + 1) == {
        "id": raised["alertId"],
        "dismissed": True,
    }
    assert plan_alerts.record(store, plan(99), 80, now=NOW + 60) == []
    assert plan_alerts.needs_you(store) == []
    with pytest.raises(plan_alerts.AlertError) as missing:
        plan_alerts.dismiss(store, "plan_" + "0" * 32)
    assert missing.value.status == 404


def test_unavailable_plans_and_bad_readings_record_nothing(store):
    assert plan_alerts.record(store, plan(99, available=False), 80, now=NOW) == []
    with pytest.raises(ValueError):
        plan_alerts.record(store, plan(140), 80, now=NOW)
    for label in ("<b>weekly</b>", "weekly\x1b[2J", "x" * 61):
        reading = plan(90)
        reading["windows"][0]["label"] = label
        with pytest.raises(ValueError):
            plan_alerts.record(store, reading, 80, now=NOW)
    assert plan_alerts.overview(store) == {"windows": [], "alerts": []}


def test_the_inbox_shows_open_alerts_and_todays_resets(store):
    plan_alerts.record(store, plan(88, provider="claude"), 80, now=NOW)
    view = inbox.inbox(store, now=NOW + 60)
    assert [item["kind"] for item in view["needsYou"]] == ["plan"]
    assert view["needsYou"][0]["title"] == "Claude weekly limit 88% used"
    friday = datetime(2026, 10, 23, 10, tzinfo=UTC).timestamp()
    plan_alerts.record(store, plan(1, NEXT_FRIDAY, provider="claude"), 80, now=friday)
    view = inbox.inbox(store, now=friday + 60)
    assert view["needsYou"] == []
    assert [item["kind"] for item in view["handled"]] == ["plan-reset"]


def test_the_worker_follows_the_owner_preference(store):
    calls = []

    def reader():
        calls.append(1)
        return plan(95)

    def broken():
        raise httpx.ConnectError("offline")

    worker = plan_alerts.PlanAlertWorker(store, lambda: [broken, reader])
    assert [change["change"] for change in worker.tick()] == ["raised"]
    current = preferences.load(store)
    off = {**current["preferences"], "planAlerts": {"enabled": False, "percent": 80}}
    preferences.save(
        store,
        preferences.UpdatePreferences(
            expected_revision=current["revision"],
            preferences=preferences.OwnerPreferences.model_validate(off),
        ),
    )
    assert worker.tick() == [] and len(calls) == 1


def test_preferences_saved_before_plan_alerts_read_the_default(store):
    with store._connect() as db:
        stored = {key: value for key, value in preferences.DEFAULT.items() if key != "planAlerts"}
        db.execute("UPDATE owner_preferences SET preferences=?", (json.dumps(stored),))
        db.commit()
    assert preferences.load(store)["preferences"]["planAlerts"] == {"enabled": True, "percent": 80}
    for percent in (49, 101):
        with pytest.raises(ValueError):
            preferences.OwnerPreferences.model_validate(
                {**preferences.DEFAULT, "planAlerts": {"enabled": True, "percent": percent}}
            )


def test_the_subscription_adapter_reads_its_runtimes_limits_route():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=plan(10))

    adapter = ChatGPTProvider("", transport=httpx.MockTransport(handler))
    assert adapter.plan_limits()["windows"][0]["usedPercent"] == 10
    assert seen == ["/limits"]


def test_plan_routes_need_the_admin_key(store):
    [raised] = plan_alerts.record(store, plan(90), 80, now=NOW)
    app = server.app
    app.state.store = store
    app.state.admin_key = "test-admin-key"
    client = TestClient(app)
    admin = {"X-Pi-Key": "test-admin-key"}
    assert client.get("/plans/usage").status_code == 401
    assert client.get("/plans/usage", headers=admin).json()["alerts"][0]["id"] == raised["alertId"]
    assert client.post("/plans/alerts/not-an-id/dismiss", headers=admin).status_code == 422
    dismissed = client.post(f"/plans/alerts/{raised['alertId']}/dismiss", headers=admin)
    assert dismissed.status_code == 200 and plan_alerts.needs_you(store) == []


def test_the_owner_key_reads_usage_but_cannot_dismiss(store, monkeypatch):
    [raised] = plan_alerts.record(store, plan(90), 80, now=NOW)
    monkeypatch.setattr(server.app.state, "store", store, raising=False)
    monkeypatch.setattr(server.app.state, "admin_key", "test-admin-key", raising=False)
    monkeypatch.setattr(
        server.app.state,
        "owner_key_hash",
        hashlib.sha256(b"test-owner-key").hexdigest(),
        raising=False,
    )
    client = TestClient(server.app)
    owner = {"X-Pi-Owner-Key": "test-owner-key"}
    usage = client.get("/plans/usage", headers=owner)
    assert usage.status_code == 200 and usage.json()["windows"][0]["usedPercent"] == 90
    assert client.get("/plans/usage", headers={"X-Pi-Owner-Key": "wrong"}).status_code == 401
    refused = client.post(f"/plans/alerts/{raised['alertId']}/dismiss", headers=owner)
    assert refused.status_code == 403 and plan_alerts.needs_you(store) != []
