"""Web push claims: which Inbox items may wake the owner's devices, each once (push_queue.py)."""

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from pi.agents import push_queue
from pi.api import server as api
from pi.conversation.store import Store
from pi.core import owner_preferences
from pi.schedule import proactive_budget

DAY = datetime(2026, 9, 21, 10, tzinfo=UTC)  # 13:00 in Jerusalem, outside quiet hours
NIGHT = datetime(2026, 9, 21, 21, tzinfo=UTC)  # midnight in Jerusalem, inside them
SECRET = "Plan the surprise for Dana"


def approval(store, title=SECRET):
    session = store.create_session(title=title)
    turn = store.start_turn(session)
    store.finish_turn(turn, "awaiting_approval", approval_args='{"tool": "mail"}')
    return session


def uncertain(store):
    session = store.create_session(title=SECRET)
    store.finish_turn(store.start_turn(session), "outcome_unknown")


def change(store, **fields):
    current = owner_preferences.load(store)
    preferences = current["preferences"]
    for name, value in fields.items():
        if name == "quiet":
            preferences["quietHours"].update(value)
        elif name == "suggestions":
            preferences["dailyBudget"]["suggestions"] = value
        else:
            preferences[name] = value
    owner_preferences.save(
        store,
        owner_preferences.UpdatePreferences(
            expected_revision=current["revision"], preferences=preferences
        ),
    )


def claim(store, now=DAY, after=0.0, limit=5):
    return push_queue.claim(store, push_queue.Claim(after=after, limit=limit), now=now)


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_an_item_is_claimed_once_and_the_claim_carries_no_words(store):
    approval(store)
    first = claim(store)
    assert [(item["kind"], item["urgent"]) for item in first["items"]] == [("approval", False)]
    assert set(first["items"][0]) == {"kind", "urgent", "tag"}
    assert SECRET not in json.dumps(first) and "mail" not in json.dumps(first)
    # A retry, a second claim, a restart: never again.
    assert claim(store)["items"] == []
    with store._connect() as db:
        rows = db.execute("SELECT * FROM push_deliveries").fetchall()
    assert len(rows) == 1 and len(rows[0]["item_key"]) == 64
    assert SECRET not in json.dumps([dict(row) for row in rows])


def test_two_claims_at_once_send_an_item_once(store):
    for _ in range(3):
        approval(store)
    results, barrier = [], threading.Barrier(4)

    def run():
        barrier.wait()
        results.append(claim(store))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    tags = [item["tag"] for result in results for item in result["items"]]
    assert len(tags) == 3 and len(set(tags)) == 3


def test_quiet_hours_hold_an_item_without_using_it_up(store):
    approval(store)
    night = claim(store, now=NIGHT)
    assert night["items"] == [] and night["held"] == ["quiet_hours"]
    assert len(claim(store, now=DAY)["items"]) == 1


def test_urgent_items_pass_quiet_hours_only_when_the_owner_allowed_it(store):
    uncertain(store)
    approval(store)
    assert claim(store, now=NIGHT)["items"] == []
    change(store, quiet={"urgentExceptions": True})
    items = claim(store, now=NIGHT)["items"]
    assert [(item["kind"], item["urgent"]) for item in items] == [("attention", True)]


def test_proactivity_off_sends_nothing_and_urgent_only_sends_only_urgent(store):
    uncertain(store)
    approval(store)
    change(store, urgency="off")
    assert claim(store)["items"] == [] and claim(store)["held"] == ["proactivity_off"]
    change(store, urgency="urgent_only")
    assert [item["urgent"] for item in claim(store)["items"]] == [True]
    assert claim(store)["held"] == ["urgent_only"]


def test_the_daily_suggestion_number_caps_pushes_per_local_day(store):
    change(store, suggestions=2)
    for _ in range(3):
        approval(store)
    assert len(claim(store)["items"]) == 2
    held = claim(store)
    assert held["items"] == [] and held["held"] == ["daily_budget_suggestions"]
    assert len(claim(store, now=DAY + timedelta(days=1))["items"]) == 1
    # Pushes never use up the reservations the owner's jobs make.
    assert proactive_budget.status(store, now=DAY)["reserved"]["suggestions"] == 0


def test_a_zero_budget_sends_nothing(store):
    change(store, suggestions=0)
    approval(store)
    assert claim(store)["items"] == []


def test_items_waiting_from_before_the_first_device_are_not_replayed(store):
    approval(store)
    after = time.time() + 1
    assert claim(store, after=after)["items"] == []
    assert len(claim(store, after=0)["items"]) == 1


def forget(db, session):
    db.execute(
        "INSERT INTO forgetting_receipts (id, root_session_id, forgotten_at, confirmation, "
        "message_count, turn_count) VALUES (?, ?, ?, ?, ?, ?)",
        ("receipt_x", session, time.time(), "test", 0, 0),
    )
    db.execute(
        "INSERT INTO forgotten_sessions (session_id, receipt_id) VALUES (?, ?)",
        (session, "receipt_x"),
    )


def test_a_forgotten_chat_never_wakes_a_device(store):
    session = approval(store)
    with store._connect() as db:
        forget(db, session)
    assert claim(store)["items"] == []


def test_a_chat_forgotten_while_the_inbox_is_read_is_never_claimed(store, monkeypatch):
    session = approval(store)
    read, landed = push_queue.inbox.needs_you_items, []

    def read_then_forget(*args):
        items = read(*args)
        # A forget that tries to land right after the Inbox was read, before the claim is written.
        with closing(sqlite3.connect(store.path, isolation_level=None, timeout=0.2)) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                forget(db, session)
                db.execute("COMMIT")
                landed.append(True)
            except sqlite3.OperationalError:  # it waits for the claim's write transaction
                pass
        return items

    monkeypatch.setattr(push_queue.inbox, "needs_you_items", read_then_forget)
    claimed = claim(store)["items"]
    assert not (landed and claimed)  # never a claim for a chat already forgotten
    assert landed or claimed


def test_unreadable_preferences_claim_nothing(store):
    approval(store)
    with store._connect() as db:
        db.execute("UPDATE owner_preferences SET preferences='{}' WHERE singleton=1")
    with pytest.raises(ValueError):
        claim(store)
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM push_deliveries").fetchone()[0] == 0


def test_claim_limits_and_shape_are_strict():
    with pytest.raises(ValueError):
        push_queue.Claim.model_validate({"after": 0, "limit": 21})
    with pytest.raises(ValueError):
        push_queue.Claim.model_validate({"after": -1})
    with pytest.raises(ValueError):
        push_queue.Claim.model_validate({"after": 0, "title": "x"})


OWNER, GATEWAY = "owner-control-test-key-0123456789", "gateway-runtime-test-key-012345678"


def test_only_the_owner_control_key_claims(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as value:
        approval(value)
        # The route claims at the wall-clock time: no quiet hours, so the hour of the run is moot.
        change(value, quiet={"enabled": False})
        state = api.app.state
        monkeypatch.setattr(state, "store", value, raising=False)
        monkeypatch.setattr(state, "admin_key", "admin-test-key", raising=False)
        for name, key in (("owner_key_hash", OWNER), ("gateway_key_hash", GATEWAY)):
            digest = hashlib.sha256(key.encode()).hexdigest()
            monkeypatch.setattr(state, name, digest, raising=False)
        client = TestClient(api.app, raise_server_exceptions=False)
        body = {"after": 0}
        assert client.post("/push/claim", json=body).status_code == 401
        refused = client.post("/push/claim", json=body, headers={"X-Pi-Gateway-Key": GATEWAY})
        assert refused.status_code in (401, 403)
        wrong = client.post("/push/claim", json=body, headers={"X-Pi-Key": "wrong"})
        assert wrong.status_code in (401, 403)
        with value._connect() as db:
            assert db.execute("SELECT count(*) FROM push_deliveries").fetchone()[0] == 0
        answer = client.post("/push/claim", json=body, headers={"X-Pi-Owner-Key": OWNER})
        assert answer.status_code == 200 and len(answer.json()["items"]) == 1
        assert answer.headers["cache-control"] == "no-store"
