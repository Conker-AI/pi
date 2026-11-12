"""Conker update offers and results: one offer per version, dismissal remembered, results once."""

import hashlib
from contextlib import closing
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi.agents import inbox
from pi.api import server
from pi.conversation.store import Store
from pi.owner import updates

NOW = datetime(2026, 11, 12, 9, tzinfo=UTC).timestamp()
RECEIPT = "0123456789abcdef0123456789abcdef"


def release(version, notes="- **Self-update.** Conker updates itself."):
    return {
        "version": version,
        "notes": notes,
        "url": f"https://github.com/Conker-AI/conker/releases/tag/v{version}",
        "publishedAt": "2026-11-12T08:00:00Z",
    }


def report(running, latest=None, results=()):
    return updates.Report.model_validate(
        {
            "running": running,
            "latest": None if latest is None else release(latest),
            "results": list(results),
        }
    )


def result(outcome, version="0.2.0", identity=RECEIPT, reason=None):
    return {
        "id": identity,
        "version": version,
        "outcome": outcome,
        "reason": reason,
        "finishedAt": NOW + 600,
    }


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_a_newer_release_is_offered_once_with_its_notes(store):
    updates.record(store, report("0.1.0", "0.2.0"), now=NOW)
    updates.record(store, report("0.1.0", "0.2.0"), now=NOW + 86400)
    [item] = updates.needs_you(store)
    assert item["kind"] == "update" and item["title"] == "Update available: 0.2.0"
    assert item["notes"].startswith("- **Self-update.**")
    assert item["since"] == NOW  # the second daily check made no second card


def test_the_running_release_or_an_older_one_is_not_offered(store):
    updates.record(store, report("0.2.0", "0.2.0"), now=NOW)
    updates.record(store, report("0.3.0", "0.2.0"), now=NOW)
    assert updates.overview(store)["offer"] is None and updates.needs_you(store) == []


def test_a_build_that_is_not_a_release_is_offered_the_latest(store):
    updates.record(store, report(None, "0.1.0"), now=NOW)
    assert updates.overview(store)["offer"]["version"] == "0.1.0"


def test_a_dismissed_version_stays_dismissed_and_a_newer_one_is_offered(store):
    updates.record(store, report("0.1.0", "0.2.0"), now=NOW)
    assert updates.dismiss_offer(store, "0.2.0", now=NOW + 1) == {
        "version": "0.2.0",
        "dismissed": True,
    }
    updates.record(store, report("0.1.0", "0.2.0"), now=NOW + 86400)
    assert updates.needs_you(store) == []
    updates.record(store, report("0.1.0", "0.3.0"), now=NOW + 2 * 86400)
    [item] = updates.needs_you(store)
    assert item["version"] == "0.3.0"


def test_only_the_newest_open_offer_is_shown(store):
    updates.record(store, report("0.1.0", "0.2.0"), now=NOW)
    updates.record(store, report("0.1.0", "0.10.0"), now=NOW + 60)
    assert [item["version"] for item in updates.needs_you(store)] == ["0.10.0"]


def test_skipping_the_newest_offer_never_brings_back_an_older_one(store):
    updates.record(store, report("0.1.0", "0.2.0"), now=NOW)
    updates.record(store, report("0.1.0", "0.10.0"), now=NOW + 60)
    updates.dismiss_offer(store, "0.10.0", now=NOW + 120)
    assert updates.needs_you(store) == []
    updates.record(store, report("0.1.0", "0.10.0"), now=NOW + 86400)
    assert updates.overview(store)["offer"] is None
    updates.record(store, report("0.1.0", "0.11.0"), now=NOW + 2 * 86400)
    assert updates.overview(store)["offer"]["version"] == "0.11.0"


def test_a_dismissed_applied_update_leaves_the_handled_list(store):
    updates.record(store, report("0.1.0", "0.2.0", [result("applied")]), now=NOW)
    assert len(updates.applied(store, NOW - 3600)) == 1
    updates.dismiss_result(store, RECEIPT, now=NOW + 60)
    assert updates.applied(store, NOW - 3600) == []


def test_running_the_offered_version_closes_its_offer(store):
    updates.record(store, report("0.1.0", "0.2.0"), now=NOW)
    updates.record(store, report("0.2.0", "0.2.0"), now=NOW + 86400)
    assert updates.overview(store)["offer"] is None


def test_a_rollback_needs_the_owner_and_is_recorded_once(store):
    updates.record(store, report("0.1.0", "0.2.0"), now=NOW)
    reason = "Health check failed after the update; the previous release is running again."
    rolled_back = result("rolled_back", reason=reason)
    updates.record(store, report("0.1.0", "0.2.0", [rolled_back]), now=NOW + 700)
    updates.record(store, report("0.1.0", "0.2.0", [rolled_back]), now=NOW + 800)
    items = updates.needs_you(store)
    assert [item["kind"] for item in items] == ["update", "update-result"]
    assert items[1]["title"] == "Update to 0.2.0 rolled back"
    assert items[1]["reason"] == reason and items[1]["outcome"] == "rolled_back"
    assert len(updates.overview(store)["results"]) == 1
    updates.dismiss_result(store, RECEIPT, now=NOW + 900)
    assert [item["kind"] for item in updates.needs_you(store)] == ["update"]


def test_a_held_update_says_a_restore_is_needed(store):
    updates.record(store, report("0.1.0", None, [result("held")]), now=NOW)
    [item] = updates.needs_you(store)
    assert item["title"] == "Update to 0.2.0 stopped: restore needed"


def test_an_applied_update_is_handled_and_closes_the_offer(store):
    updates.record(store, report("0.1.0", "0.2.0"), now=NOW)
    updates.record(store, report("0.1.0", None, [result("applied")]), now=NOW + 700)
    assert updates.needs_you(store) == []
    [handled] = updates.applied(store, NOW)
    assert handled["title"] == "Updated to 0.2.0" and handled["kind"] == "update-applied"
    assert updates.overview(store)["results"][0]["outcome"] == "applied"


def test_the_inbox_lists_the_offer_and_the_applied_update(store):
    updates.record(store, report("0.1.0", "0.3.0", [result("applied")]), now=NOW)
    view = inbox.inbox(store, now=NOW + 3600)
    assert any(item["kind"] == "update" for item in view["needsYou"])
    assert any(item["kind"] == "update-applied" for item in view["handled"])


@pytest.mark.parametrize(
    "field",
    [
        {"running": "v0.1.0"},
        {"latest": {**release("0.2.0"), "url": "https://example.com/releases/tag/v0.2.0"}},
        {"latest": {**release("0.2.0"), "notes": "x" * 20001}},
        {"results": [{**result("applied"), "outcome": "done"}]},
        {"results": [{**result("applied"), "id": "../receipt"}]},
        {"extra": True},
    ],
)
def test_a_report_with_a_wrong_shape_is_refused(field):
    with pytest.raises(ValidationError):
        updates.Report.model_validate({"running": "0.1.0", **field})


def test_dismissing_an_unknown_version_or_result_is_refused(store):
    with pytest.raises(updates.UpdateError) as missing:
        updates.dismiss_offer(store, "9.9.9")
    assert missing.value.status == 404
    with pytest.raises(updates.UpdateError) as invalid:
        updates.dismiss_result(store, "nope")
    assert invalid.value.status == 422


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(server.app.state, "store", store, raising=False)
    monkeypatch.setattr(server.app.state, "admin_key", "test-admin-key", raising=False)
    monkeypatch.setattr(
        server.app.state,
        "owner_key_hash",
        hashlib.sha256(b"test-owner-key").hexdigest(),
        raising=False,
    )
    return TestClient(server.app)


OWNER = {"X-Pi-Owner-Key": "test-owner-key"}
ADMIN = {"X-Pi-Key": "test-admin-key"}


def test_only_the_recovery_key_reports_and_the_owner_key_reads_and_dismisses(client, store):
    body = {"running": "0.1.0", "latest": release("0.2.0"), "results": []}
    refused = client.post("/updates/report", json=body, headers=OWNER)
    assert refused.status_code in (401, 403) and updates.overview(store)["offer"] is None
    assert client.post("/updates/report", json=body).status_code == 401
    reported = client.post("/updates/report", json=body, headers=ADMIN)
    assert reported.status_code == 200 and reported.json()["offer"]["version"] == "0.2.0"
    read = client.get("/updates", headers=OWNER)
    assert read.status_code == 200 and read.json()["offer"]["title"] == "Update available: 0.2.0"
    dismissed = client.post("/updates/offers/0.2.0/dismiss", headers=OWNER)
    assert dismissed.status_code == 200 and updates.overview(store)["offer"] is None
    assert client.post("/updates/offers/0.2/dismiss", headers=OWNER).status_code == 403
    assert client.post("/updates/offers/0.2/dismiss", headers=ADMIN).status_code == 422
    assert client.post("/updates/offers/9.9.9/dismiss", headers=OWNER).status_code == 404


def test_the_owner_key_dismisses_a_result(client, store):
    updates.record(store, report("0.1.0", None, [result("rolled_back")]), now=NOW)
    response = client.post(f"/updates/results/{RECEIPT}/dismiss", headers=OWNER)
    assert response.status_code == 200 and updates.overview(store)["results"] == []
