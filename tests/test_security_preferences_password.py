"""Security preferences change only with the owner's password (ADR-0010, 2026-11-06 decision).

The sign-out time (`idleTimeoutMinutes`), the access a new chat starts with (`newChatAccess`) and
the daily budgets (`dailyBudget`) are, like the spending cap (tests/test_spending_cap_password.py),
settings the browser asks the password for. The host CLI (`conker config set`) saved them with
Pi's recovery key, which asks for nothing; Pi now refuses a change to them on that key, and the
gateway's `owner-preferences` host command (tests/test_gateway_host_preferences.py) checks the
password and saves with the owner credential instead. Every other preference still saves on the key.
"""

import copy
import hashlib
import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.conversation.store import Store
from pi.core import owner_preferences as prefs

OWNER = "security-preferences-owner-key-" + "o" * 32
RECOVERY = "security-preferences-recovery-key"
ROUTE = "/owner/preferences"
# One change per protected setting; every value is valid, so only the password is missing.
PROTECTED = [
    ("idleTimeoutMinutes", 60),
    ("idleTimeoutMinutes", 0),  # "the server's limit" is the loosest choice
    ("newChatAccess", "full"),
    ("newChatAccess", "ask"),  # tightening still asks, as it does in the browser
    ("dailyBudget.costCents", 100_000),
    ("dailyBudget.suggestions", 100),
    ("dailyBudget.researchMinutes", 1440),
]
# What the host CLI keeps saving on the recovery key.
OPEN = [
    ("quietHours.enabled", False),
    ("quietHours.timeZone", "Europe/London"),
    ("urgency", "off"),
    ("planAlerts.percent", 50),
]


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


@pytest.fixture
def pi(monkeypatch, store):
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", RECOVERY, raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    return TestClient(api.app)


def document(store, field, value):
    current = prefs.load(store)
    preferences = copy.deepcopy(current["preferences"])
    *parents, leaf = field.split(".")
    target = preferences
    for name in parents:
        target = target[name]
    target[leaf] = value
    return {"expected_revision": current["revision"], "preferences": preferences}


@pytest.mark.parametrize(("field", "value"), PROTECTED)
def test_the_recovery_key_cannot_change_a_security_preference(pi, store, field, value):
    before = prefs.load(store)
    refused = pi.post(ROUTE, headers={"X-Pi-Key": RECOVERY}, json=document(store, field, value))
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "password_required"
    assert json.dumps(value) not in refused.text  # never echoes the body
    assert prefs.load(store) == before


@pytest.mark.parametrize(("field", "value"), PROTECTED)
def test_the_owner_credential_changes_it(pi, store, field, value):
    saved = pi.post(ROUTE, headers={"X-Pi-Owner-Key": OWNER}, json=document(store, field, value))
    assert saved.status_code == 200, saved.text
    assert prefs.load(store) == {"revision": 2, "preferences": saved.json()["preferences"]}


@pytest.mark.parametrize(("field", "value"), OPEN)
def test_the_recovery_key_still_saves_every_other_preference(pi, store, field, value):
    body = document(store, field, value)
    saved = pi.post(ROUTE, headers={"X-Pi-Key": RECOVERY}, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"revision": 2, "preferences": body["preferences"]}


def test_a_save_that_also_changes_a_security_preference_saves_nothing(pi, store):
    body = document(store, "urgency", "off")
    body["preferences"]["newChatAccess"] = "full"
    before = prefs.load(store)
    assert pi.post(ROUTE, headers={"X-Pi-Key": RECOVERY}, json=body).status_code == 403
    assert prefs.load(store) == before


def stored_row(store):
    with store._connect() as db:
        return json.loads(db.execute("SELECT preferences FROM owner_preferences").fetchone()[0])


def test_a_setting_stored_before_it_existed_reads_as_its_default(pi, store):
    # Preferences saved before newChatAccess existed read as its default: loosening it from there
    # is a change (judged against the default, never against the request), keeping it is not.
    stored = stored_row(store)
    stored.pop("newChatAccess")
    with store._connect() as db:
        db.execute("UPDATE owner_preferences SET preferences=?", (json.dumps(stored),))
        db.commit()
    loosened = document(store, "newChatAccess", "full")
    assert pi.post(ROUTE, headers={"X-Pi-Key": RECOVERY}, json=loosened).status_code == 403
    assert "newChatAccess" not in stored_row(store)  # nothing was saved
    kept = pi.post(ROUTE, headers={"X-Pi-Key": RECOVERY}, json=document(store, "urgency", "off"))
    assert kept.status_code == 200, kept.text
    assert stored_row(store)["newChatAccess"] == "default"
