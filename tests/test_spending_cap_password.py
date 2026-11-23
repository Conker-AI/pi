"""The daily spending cap changes only with the owner's password (R-M6, ADR-0010).

The browser asks for it at the gateway (`/owner/preferences` is a password write). The host CLI
used to save the cap with Pi's recovery key, which asks for nothing; Pi now refuses a cap change
made with that key. The gateway's `spending-cap` host command (tests/test_gateway_spending_cap.py)
checks the password and saves with the owner credential instead.
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

OWNER = "spending-cap-owner-key-" + "o" * 32
RECOVERY = "spending-cap-recovery-key"
ROUTE = "/owner/preferences"


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


def document(store, **cap):
    current = prefs.load(store)
    preferences = copy.deepcopy(current["preferences"])
    preferences["spendingCap"] = {**preferences["spendingCap"], **cap}
    return {"expected_revision": current["revision"], "preferences": preferences}


@pytest.mark.parametrize(
    "change", [{"cents": 100_000}, {"enabled": False}, {"tokens": 100_000_000}, {"cents": 0}]
)
def test_the_recovery_key_cannot_change_the_spending_cap(pi, store, change):
    before = prefs.load(store)
    refused = pi.post(ROUTE, headers={"X-Pi-Key": RECOVERY}, json=document(store, **change))
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "password_required"
    assert "100000" not in refused.text  # the refusal never echoes the body
    assert prefs.load(store) == before


def test_the_recovery_key_still_saves_other_preferences_and_the_owner_key_the_cap(pi, store):
    body = document(store)
    body["preferences"]["urgency"] = "off"
    saved = pi.post(ROUTE, headers={"X-Pi-Key": RECOVERY}, json=body)
    assert saved.status_code == 200 and saved.json()["preferences"]["urgency"] == "off"
    # Stored before spendingCap existed: the default it reads as is not a change either.
    with store._connect() as db:
        stored = json.loads(db.execute("SELECT preferences FROM owner_preferences").fetchone()[0])
        stored.pop("spendingCap")
        db.execute("UPDATE owner_preferences SET preferences=?", (json.dumps(stored),))
        db.commit()
    assert pi.post(ROUTE, headers={"X-Pi-Key": RECOVERY}, json=document(store)).status_code == 200
    # The owner credential is the gateway, which asked for the password first.
    raised = pi.post(ROUTE, headers={"X-Pi-Owner-Key": OWNER}, json=document(store, cents=900))
    assert raised.status_code == 200 and raised.json()["preferences"]["spendingCap"]["cents"] == 900
    # Which credential a request used is its own: the next recovery-key change is refused again.
    again = pi.post(ROUTE, headers={"X-Pi-Key": RECOVERY}, json=document(store, cents=100_000))
    assert again.status_code == 403
    assert prefs.load(store)["preferences"]["spendingCap"]["cents"] == 900


def test_a_concurrent_cap_change_is_judged_against_the_stored_cap(store):
    # The check runs inside the save's own transaction: a body that keeps the cap it read is
    # refused by revision, never admitted against a cap changed meanwhile.
    stale = prefs.UpdatePreferences.model_validate(document(store))
    prefs.save(store, prefs.UpdatePreferences.model_validate(document(store, cents=900)))
    with pytest.raises(prefs.RevisionConflict):
        prefs.save(store, stale, keep_cap=True)
    current = prefs.UpdatePreferences.model_validate(document(store, cents=500))
    with pytest.raises(prefs.CapNeedsPassword):
        prefs.save(store, current, keep_cap=True)
    assert prefs.load(store)["preferences"]["spendingCap"]["cents"] == 900
