"""GET /insights/quiet (R-M11): counts of what asked for the owner and what did not, in two equal
windows; hidden chats count for nothing; only counts leave Pi, never his words."""

from __future__ import annotations

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.agents import insights
from pi.api.browser_contract import owner_allowed
from pi.conversation import archive
from pi.conversation.store import Store

OWNER = "synthetic-owner"
NOW = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


@pytest.fixture
def client(store, monkeypatch):
    from pi.api import server as api

    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    return TestClient(api.app)


def handled(store, session, at):
    with store._connect() as db:
        db.execute(
            "INSERT INTO handled_actions VALUES (?,?,?,?,?,?,?,?)",
            (f"h{at}{session}", session, None, None, "tool", "ai", "because", at),
        )
        db.commit()


def request(store, session, at, state="open", decided=None, ident=None):
    with store._connect() as db:
        db.execute(
            "INSERT INTO change_requests(id,kind,title,summary,payload,session_id,state,created_at,"
            "decided_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                ident or f"c{at}{state}{session}",
                "memory",
                "t",
                "s",
                "{}",
                session,
                state,
                at,
                decided,
            ),
        )
        db.commit()


def ask(store, session, at, state="open", answered=None):
    with store._connect() as db:
        db.execute(
            "INSERT INTO owner_asks(id,session_id,question,options,state,created_at,answered_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (f"q{at}{state}", session, "q", "[]", state, at, answered),
        )
        db.commit()


def test_two_windows_count_what_asked_and_what_did_not(store):
    session = store.create_session(title="Work")
    for age in (1, 2, 3, 4):
        handled(store, session, NOW - age * DAY)
    handled(store, session, NOW - 9 * DAY)  # previous window
    request(store, session, NOW - 2 * DAY, "approved", NOW - 2 * DAY + 60)
    request(store, session, NOW - 3 * DAY, "declined", NOW - 3 * DAY + 120)
    request(store, session, NOW - 5 * DAY, "open")
    ask(store, session, NOW - 4 * DAY, "answered", NOW - 4 * DAY + 600)
    ask(store, session, NOW - 10 * DAY, "dismissed", NOW - 10 * DAY + 5)  # previous window
    page = insights.quiet(store, NOW, 7)
    now, before = page["current"], page["previous"]
    insights.QuietInsights.model_validate(page)
    assert (now["handledWithoutAsking"], now["asked"]) == (4, 4)
    assert (now["changeRequestsRaised"], now["questionsAsked"], now["noticesRaised"]) == (3, 1, 0)
    assert (now["approved"], now["declined"], now["failed"]) == (1, 1, 0)
    assert (now["questionsAnswered"], now["questionsDismissed"]) == (1, 0)
    assert now["medianAnswerSeconds"] == 120.0  # waits of 60, 120 and 600 seconds
    assert now["quietShare"] == 0.5
    assert (before["handledWithoutAsking"], before["asked"], before["questionsDismissed"]) == (
        1,
        1,
        1,
    )
    assert before["quietShare"] == 0.5
    assert (page["days"], now["until"], before["until"]) == (7, NOW, NOW - 7 * DAY)


def test_an_empty_database_counts_zero_and_has_no_share_or_median(store):
    now = insights.quiet(store, NOW, 7)["current"]
    assert (now["asked"], now["handledWithoutAsking"]) == (0, 0)
    assert now["quietShare"] is None and now["medianAnswerSeconds"] is None


def test_a_trashed_chat_counts_for_nothing_until_restored(store):
    shown = store.create_session(title="Work")
    hidden = store.create_session(title="Private")
    for session in (shown, hidden):
        handled(store, session, NOW - DAY)
        request(store, session, NOW - DAY)
    archive.trash(store, archive.Trash(sessions=[hidden], trashed=True))
    now = insights.quiet(store, NOW, 7)["current"]
    assert (now["handledWithoutAsking"], now["changeRequestsRaised"]) == (1, 1)
    archive.trash(store, archive.Trash(sessions=[hidden], trashed=False))
    now = insights.quiet(store, NOW, 7)["current"]
    assert (now["handledWithoutAsking"], now["changeRequestsRaised"]) == (2, 2)


def test_the_route_is_owner_only_validates_days_and_names_its_model(client, store):
    assert client.get("/insights/quiet").status_code == 401
    owner = {"X-Pi-Owner-Key": OWNER}
    body = client.get("/insights/quiet?days=14", headers=owner)
    assert body.status_code == 200 and body.json()["days"] == 14
    insights.QuietInsights.model_validate(body.json())
    for days in (0, 91):
        assert client.get(f"/insights/quiet?days={days}", headers=owner).status_code == 422
    assert owner_allowed("GET", "/insights/quiet") and not owner_allowed("POST", "/insights/quiet")
    schema = client.get("/openapi.json").json()["paths"]["/insights/quiet"]["get"]["responses"]
    assert schema["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/QuietInsights"
    }
