"""Pi checks itself and files an Inbox card when a part stays broken (pi.owner.self_check)."""

import hashlib
import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.agents import inbox
from pi.api import browser_contract
from pi.api import server as api
from pi.conversation.store import Store
from pi.owner import self_check

OWNER = "synthetic-owner-key-" + "s" * 32
DOWN = {"status": "unavailable", "reason": "ConnectError"}
UP = {"status": "ok"}
T0 = 1_800_000_000.0
LATER = T0 + self_check.PERSIST_SECONDS


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_a_blip_raises_nothing_and_a_lasting_failure_raises_one_alert(store):
    assert self_check.record(store, {"memory": DOWN}, now=T0) == []
    assert self_check.record(store, {"memory": UP}, now=T0 + 60) == []
    assert self_check.record(store, {"memory": DOWN}, now=T0 + 120) == []
    assert self_check.record(store, {"memory": DOWN}, now=LATER) == []  # 780 s, not yet
    [raised] = self_check.record(store, {"memory": DOWN}, now=LATER + 120)
    assert raised["change"] == "raised"
    [item] = self_check.needs_you(store)
    assert item == {
        "kind": "health",
        "id": raised["alertId"],
        "title": "Memory is not working",
        "since": T0 + 120,
        "status": "unavailable",
        "reason": "ConnectError",
        "source": {"kind": "self-check", "alertId": raised["alertId"], "check": "memory"},
    }
    assert self_check.record(store, {"memory": DOWN}, now=LATER + 600) == []  # still one


def test_a_check_that_works_again_closes_its_alert_and_says_so(store):
    self_check.record(store, {"action_boundary": DOWN}, now=T0)
    [raised] = self_check.record(store, {"action_boundary": DOWN}, now=LATER)
    [cleared] = self_check.record(store, {"action_boundary": UP}, now=LATER + 300)
    assert cleared == {"change": "cleared", "alertId": raised["alertId"]}
    assert self_check.needs_you(store) == []
    [back] = self_check.recovered(store, T0)
    assert back["kind"] == "health-recovered" and back["title"] == "Tools (ToolGate) works again"


@pytest.mark.parametrize("status", ["ok", "not_configured", "unknown"])
def test_working_unset_and_unproven_checks_never_raise(store, status):
    self_check.record(store, {"hosted_provider": {"status": status}}, now=T0)
    assert self_check.record(store, {"hosted_provider": {"status": status}}, now=LATER * 2) == []


def test_a_dismissed_alert_stays_hidden_until_the_check_recovers_and_breaks_again(store):
    degraded = {"status": "degraded"}
    self_check.record(store, {"speech": degraded}, now=T0)
    [raised] = self_check.record(store, {"speech": degraded}, now=LATER)
    assert self_check.dismiss(store, raised["alertId"]) == {
        "id": raised["alertId"],
        "dismissed": True,
    }
    assert self_check.record(store, {"speech": degraded}, now=LATER + 900) == []
    assert self_check.needs_you(store) == []
    self_check.record(store, {"speech": UP}, now=LATER + 1000)
    self_check.record(store, {"speech": degraded}, now=LATER + 1100)
    [again] = self_check.record(store, {"speech": degraded}, now=LATER * 2)
    assert again["change"] == "raised" and len(self_check.needs_you(store)) == 1
    with pytest.raises(self_check.SelfCheckError):
        self_check.dismiss(store, "sc_" + "0" * 32)


def test_rows_hold_names_statuses_and_short_reasons_only(store):
    long = {"status": "unavailable", "reason": "x" * 1000}
    self_check.record(store, {"memory": long, "odd": "not a check", 7: UP}, now=T0)
    self_check.record(store, {"memory": long}, now=LATER)
    [item] = self_check.needs_you(store)
    assert len(item["reason"]) == self_check.REASON_CHARS
    view = self_check.overview(store)
    assert [check["check"] for check in view["checks"]] == ["memory"]
    assert view["checks"][0]["name"] == "Memory" and view["alerts"] == [item]


def test_the_inbox_shows_open_alerts_and_todays_recoveries(store):
    now = time.time()
    self_check.record(store, {"store": DOWN, "memory": DOWN}, now=now - 2000)
    self_check.record(store, {"store": DOWN, "memory": DOWN}, now=now - 1000)
    self_check.record(store, {"store": DOWN, "memory": UP}, now=now - 10)
    view = inbox.inbox(store, now=now)
    titles = [item["title"] for item in view["needsYou"]]
    handled = [item["title"] for item in view["handled"]]
    assert "Pi's database is not working" in titles and "Memory works again" in handled


def test_the_worker_records_what_the_health_probe_reports(store):
    worker = self_check.SelfCheckWorker(store, lambda: {"checks": {"memory": DOWN}})
    assert worker.tick() == []
    assert self_check.overview(store)["checks"][0]["status"] == "unavailable"
    with pytest.raises(ValueError):
        self_check.SelfCheckWorker(store, lambda: {}, check_seconds=0)


def test_the_owner_reads_and_dismisses_from_the_browser_with_the_session(store, monkeypatch):
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin-key", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    self_check.record(store, {"memory": DOWN}, now=T0)
    [raised] = self_check.record(store, {"memory": DOWN}, now=LATER)
    http, owner = TestClient(api.app), {"X-Pi-Owner-Key": OWNER}
    assert http.get("/self-check").status_code == 401
    assert http.get("/self-check", headers=owner).json()["alerts"][0]["id"] == raised["alertId"]
    path = f"/self-check/alerts/{raised['alertId']}/dismiss"
    assert http.post(path, headers=owner).json()["dismissed"] is True
    assert http.post("/self-check/alerts/sc_bad/dismiss", headers=owner).status_code == 403
    assert browser_contract.owner_allowed("GET", "/self-check")
    assert browser_contract.owner_allowed("POST", path)
    assert browser_contract.session_only_write("POST", path)
    assert not browser_contract.runtime_allowed("GET", "/self-check")
    assert api.app.state.health_probe is api.health
