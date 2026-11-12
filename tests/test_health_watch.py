"""Health watch: two failures in a row file one Inbox card; working again closes it."""

from contextlib import closing
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from pi.agents import inbox
from pi.api import health_probes, server
from pi.conversation.store import Store
from pi.owner import health_watch
from pi.owner import settings as owner_settings

NOW = datetime(2026, 11, 12, 14, 0, tzinfo=UTC).timestamp()
DOWN = health_watch.broken("Web search is not working: the search service does not answer.")
# The keys of the Inbox's continuity attention items, which the health cards reuse.
ATTENTION_KEYS = {"kind", "id", "title", "since", "source", "urgency", "status"}


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def turn_off(store):
    owner_settings.save(
        store, owner_settings.Update(expected_revision=0, values={health_watch.SETTING: False})
    )


def test_one_failure_files_nothing(store):
    assert health_watch.record(store, "web_search", DOWN, now=NOW) is None
    assert health_watch.needs_you(store) == []


def test_two_failures_file_one_card_with_a_plain_reason(store):
    health_watch.record(store, "web_search", DOWN, now=NOW)
    change = health_watch.record(store, "web_search", DOWN, now=NOW + 900)
    assert change["change"] == "raised" and change["check"] == "web_search"
    [card] = health_watch.needs_you(store)
    assert card["title"] == "Web search is not working: the search service does not answer."
    assert card["since"] == NOW + 900 and card["id"] == change["alertId"]


def test_a_third_failure_does_not_duplicate(store):
    for step in range(4):
        health_watch.record(store, "web_search", DOWN, now=NOW + step * 900)
    assert len(health_watch.needs_you(store)) == 1


def test_a_blip_between_failures_resets_the_count(store):
    health_watch.record(store, "web_search", DOWN, now=NOW)
    health_watch.record(store, "web_search", health_watch.WORKS, now=NOW + 900)
    assert health_watch.record(store, "web_search", DOWN, now=NOW + 1800) is None
    assert health_watch.needs_you(store) == []


def test_unknown_changes_nothing(store):
    health_watch.record(store, "toolgate", DOWN, now=NOW)
    assert health_watch.record(store, "toolgate", None, now=NOW + 900) is None
    assert health_watch.record(store, "toolgate", DOWN, now=NOW + 1800)["change"] == "raised"


def test_recovery_resolves_the_card_and_it_shows_as_handled(store):
    health_watch.record(store, "web_search", DOWN, now=NOW)
    raised = health_watch.record(store, "web_search", DOWN, now=NOW + 900)
    fixed = health_watch.record(store, "web_search", health_watch.WORKS, now=NOW + 1200)
    assert fixed == {"change": "fixed", "alertId": raised["alertId"], "check": "web_search"}
    assert health_watch.needs_you(store) == []
    [done] = health_watch.fixes(store, NOW, UTC)
    assert done["title"] == "Web search works again (fixed at 14:20)"
    assert done["at"] == NOW + 1200
    # Broken again later: a new card, not the old one.
    health_watch.record(store, "web_search", DOWN, now=NOW + 2000)
    again = health_watch.record(store, "web_search", DOWN, now=NOW + 3000)
    assert again["change"] == "raised" and again["alertId"] != raised["alertId"]


def test_dismiss_hides_the_card_until_it_works_again(store):
    health_watch.record(store, "toolgate", DOWN, now=NOW)
    raised = health_watch.record(store, "toolgate", DOWN, now=NOW + 900)
    assert health_watch.dismiss(store, raised["alertId"]) == {
        "id": raised["alertId"],
        "dismissed": True,
    }
    assert health_watch.needs_you(store) == []
    assert health_watch.record(store, "toolgate", DOWN, now=NOW + 1800) is None
    assert health_watch.needs_you(store) == []
    with pytest.raises(health_watch.HealthWatchError):
        health_watch.dismiss(store, "health_" + "0" * 32)


def test_setting_off_files_nothing(store):
    turn_off(store)
    worker = health_watch.HealthWatchWorker(store, lambda: {"web_search": lambda: DOWN})
    assert worker.tick() == [] and worker.tick() == []
    assert health_watch.needs_you(store) == []
    assert health_watch.overview(store)["enabled"] is False


def test_the_worker_files_after_two_rounds(store):
    worker = health_watch.HealthWatchWorker(store, lambda: {"web_search": lambda: DOWN})
    assert worker.tick() == []
    [change] = worker.tick()
    assert change["change"] == "raised"


def test_the_card_is_an_attention_item_in_the_inbox(store):
    health_watch.record(store, "memorygate", DOWN, now=NOW)
    health_watch.record(store, "memorygate", DOWN, now=NOW + 900)
    view = inbox.inbox(store, now=NOW + 1000)
    [card] = [item for item in view["needsYou"] if item["source"]["kind"] == "health-alert"]
    assert set(card) == ATTENTION_KEYS
    assert card["kind"] == "attention" and card["status"] == "failed"
    assert card["urgency"] == {"urgent": False, "basis": "health-check"}
    health_watch.record(store, "memorygate", health_watch.WORKS, now=NOW + 1100)
    view = inbox.inbox(store, now=NOW + 1200)
    assert view["needsYou"] == []
    zone, _ = inbox._owner_zone(store)
    local = datetime.fromtimestamp(NOW + 1100, zone)
    assert [item["title"] for item in view["handled"]] == [
        f"Long-term memory works again (fixed at {local:%H:%M})"
    ]


def test_the_worker_never_raises_out_of_tick(store):
    def explode():
        raise RuntimeError("probe blew up")

    worker = health_watch.HealthWatchWorker(
        store, lambda: {"toolgate": explode, "web_search": lambda: DOWN}
    )
    assert worker.tick() == []
    assert worker.tick()[0]["check"] == "web_search"

    def no_probes():
        raise RuntimeError("state is gone")

    assert health_watch.HealthWatchWorker(store, no_probes).tick() == []
    unknown = health_watch.HealthWatchWorker(store, lambda: {"made_up": lambda: DOWN})
    assert unknown.tick() == []


def test_reasons_are_one_bounded_line(store):
    long = health_watch.broken("first line\nsecond line " + "x" * 400)
    health_watch.record(store, "toolgate", long, now=NOW)
    health_watch.record(store, "toolgate", long, now=NOW + 900)
    [card] = health_watch.needs_you(store)
    assert "\n" not in card["title"] and len(card["title"]) <= health_watch.MAX_REASON


def test_the_routes_need_the_admin_key(store, monkeypatch):
    health_watch.record(store, "toolgate", DOWN, now=NOW)
    raised = health_watch.record(store, "toolgate", DOWN, now=NOW + 900)
    monkeypatch.setattr(server.app.state, "store", store, raising=False)
    monkeypatch.setattr(server.app.state, "admin_key", "test-admin-key", raising=False)
    client = TestClient(server.app)
    admin = {"X-Pi-Key": "test-admin-key"}
    assert client.get("/health-watch").status_code == 401
    view = client.get("/health-watch", headers=admin).json()
    assert view["checks"][0]["check"] == "toolgate" and view["checks"][0]["works"] is False
    assert view["alerts"][0]["id"] == raised["alertId"]
    assert client.post("/health-watch/alerts/nope/dismiss", headers=admin).status_code == 422
    dismissed = client.post(f"/health-watch/alerts/{raised['alertId']}/dismiss", headers=admin)
    assert dismissed.status_code == 200 and health_watch.needs_you(store) == []


# Probes, with fakes: each reads a health signal and answers in plain words.


class FakeToolGate:
    base_url = "http://toolgate.test"

    def __init__(self, state):
        self.state = state

    def health(self):
        return self.state


def test_toolgate_probe():
    assert health_probes.toolgate(None) is None
    assert health_probes.toolgate(FakeToolGate({"status": "ok"})) == health_watch.WORKS
    assert health_probes.toolgate(FakeToolGate({"status": "degraded", "reason": "lockdown"})) == (
        health_watch.WORKS
    )
    assert health_probes.toolgate(FakeToolGate({"status": "not_configured"})) is None
    works, reason = health_probes.toolgate(
        FakeToolGate({"status": "unavailable", "reason": "ConnectError"})
    )
    assert works is False and "cannot reach ToolGate" in reason and "ConnectError" not in reason
    works, reason = health_probes.toolgate(
        FakeToolGate({"status": "unavailable", "reason": "execution key rejected"})
    )
    assert works is False and "refuses Conker's key" in reason


def test_web_search_probe_reads_toolgates_health_page(monkeypatch):
    answers = {}

    def fake_get(url, **_):
        assert url == "http://toolgate.test/health"
        if "error" in answers:
            raise httpx.ConnectError("down")
        body = {"checks": {"searxng": {"status": answers["status"]}}}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(health_probes.httpx, "get", fake_get)
    client = FakeToolGate({"status": "ok"})
    answers["status"] = "ok"
    assert health_probes.web_search(client) == health_watch.WORKS
    answers["status"] = "unavailable"
    assert health_probes.web_search(client)[0] is False
    answers["status"] = "not_configured"
    assert health_probes.web_search(client) is None
    answers["error"] = True
    assert health_probes.web_search(client) is None
    assert health_probes.web_search(None) is None


class FakeMemory:
    def __init__(self, status=None):
        self.client = None if status is None else FakeToolGate({"status": status})


def test_memorygate_probe():
    assert health_probes.memorygate(FakeMemory()) is None
    assert health_probes.memorygate(FakeMemory("ok")) == health_watch.WORKS
    assert health_probes.memorygate(FakeMemory("degraded"))[0] is False
    works, reason = health_probes.memorygate(FakeMemory("unavailable"))
    assert works is False and "not remembered" in reason


def test_memory_delivery_probe(store):
    assert health_probes.memory_delivery(store, FakeMemory()) is None
    assert health_probes.memory_delivery(store, FakeMemory("ok")) == health_watch.WORKS
    with store._connect() as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO memory_outbox(message_id,operation,state,attempts) "
            "VALUES ('msg_a','ingest','pending',?)",
            (health_probes.STUCK_ATTEMPTS,),
        )
        db.commit()
    works, reason = health_probes.memory_delivery(store, FakeMemory("ok"))
    assert works is False and "keeps failing" in reason
    with store._connect() as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("UPDATE memory_outbox SET state='blocked'")
        db.commit()
    assert "blocked" in health_probes.memory_delivery(store, FakeMemory("ok"))[1]


class FakeAdapter:
    def __init__(self, status):
        self.status = status

    def health(self):
        return {"status": self.status}


def configuration(model_id="m1"):
    return {
        "configuration": {
            "providers": [{"id": "openrouter", "name": "OpenRouter", "enabled": True}],
            "models": [
                {
                    "id": "m1",
                    "providerId": "openrouter",
                    "name": "Big Model",
                    "route": "vendor/big",
                    "enabled": True,
                }
            ],
            "roleSettings": {
                "roles": {"answer": {"enabled": True, "modelId": model_id, "eligibleModelIds": []}}
            },
        }
    }


def test_answer_model_probe_reads_provider_health_only(store, monkeypatch):
    monkeypatch.setattr(health_probes.model_roles, "load", lambda _: {"configuration": None})
    assert health_probes.answer_model(store, dict) is None
    monkeypatch.setattr(health_probes.model_roles, "load", lambda _: configuration())
    for status in ("ok", "unverified"):
        adapters = {"openrouter": FakeAdapter(status)}
        assert health_probes.answer_model(store, lambda a=adapters: a) == health_watch.WORKS
    works, reason = health_probes.answer_model(
        store, lambda: {"openrouter": FakeAdapter("unavailable")}
    )
    assert works is False and "Big Model" in reason
    works, reason = health_probes.answer_model(
        store, lambda: {"openrouter": FakeAdapter("not_configured")}
    )
    assert works is False and "not set up" in reason
    assert health_probes.answer_model(store, dict)[0] is False
    monkeypatch.setattr(health_probes.model_roles, "load", lambda _: configuration(None))
    assert health_probes.answer_model(store, dict) is None
