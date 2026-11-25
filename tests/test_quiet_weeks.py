"""GET /insights/quiet/weeks (R-M11 / R-E13): per ISO week in the owner's zone, what Conker raised
for him and what became of it; hidden chats count for nothing; only counts leave Pi."""

from __future__ import annotations

import hashlib
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from pi.agents import push_queue, quiet_weeks
from pi.api.browser_contract import owner_allowed
from pi.conversation import archive
from pi.conversation.store import Store

OWNER = "synthetic-owner"
ZONE = "America/Los_Angeles"
# Wednesday 2026-11-25 12:00 in Los Angeles: ISO week 2026-W48 runs from Monday 2026-11-23.
NOW = datetime(2026, 11, 25, 12, tzinfo=ZoneInfo(ZONE)).timestamp()


def at(day: int, hour: int, zone: str = ZONE) -> float:
    return datetime(2026, 11, day, hour, tzinfo=ZoneInfo(zone)).timestamp()


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        with value._connect() as db:
            db.execute(
                "UPDATE owner_preferences SET preferences="
                "json_set(preferences,'$.quietHours.timeZone',?)",
                (ZONE,),
            )
            db.commit()
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


def write(store, sql, args):
    with store._connect() as db:
        db.execute(sql, args)
        db.commit()


def ask(store, ident, session, when, state="open"):
    write(
        store,
        "INSERT INTO owner_asks(id,session_id,question,options,state,created_at) VALUES (?,?,?,?,?,?)",
        (ident, session, "q", "[]", state, when),
    )


def request(store, ident, session, when, state="open", kind="memory"):
    write(
        store,
        "INSERT INTO change_requests(id,kind,title,summary,payload,session_id,state,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ident, kind, "t", "s", "{}", session, state, when),
    )


def proposal(store, ident, when, state="open"):
    write(
        store,
        "INSERT OR IGNORE INTO proposal_passes(id,started_at,state) VALUES ('pass',?,'done')",
        (when,),
    )
    write(
        store,
        "INSERT INTO proposals(id,pass_id,fingerprint,title,noticed,suggestion,if_approved,"
        "evidence,state,created_at) VALUES (?,'pass',?,'t','n','s','i','[]',?,?)",
        (ident, ident, state, when),
    )


def notice(store, ident, kind, when, session=None, dismissed=None):
    write(
        store,
        "INSERT INTO notices(id,kind,subject,title,session_id,created_at,dismissed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (ident, kind, ident, "t", session, when, dismissed),
    )


def approval(store, turn, session, when):
    write(
        store,
        "INSERT INTO turns(id,session_id,status,started_at) VALUES (?,?,'awaiting_approval',?)",
        (turn, session, when),
    )
    write(store, "INSERT INTO turn_approval_requests VALUES (?,?)", (turn, "req_" + turn))


def pushed(store, kind, ident, when):
    key = push_queue.item_key({"kind": kind, "id": ident})
    write(store, "INSERT INTO push_deliveries VALUES (?,?)", (key, when))


def weeks(store, count=3):
    page = quiet_weeks.quiet_weeks(store, NOW, count)
    quiet_weeks.QuietWeeks.model_validate(page)
    return page


def test_each_week_holds_what_was_raised_in_it_in_the_owners_zone(store):
    chat = store.create_session(title="Work")
    # Monday 03:00 UTC is still Sunday evening in Los Angeles: last week, not this one.
    ask(store, "q_sunday", chat, at(23, 3, "UTC"), "answered")
    ask(store, "q_monday", chat, at(23, 9), "dismissed")
    ask(store, "q_open", chat, at(24, 9))
    request(store, "c_yes", chat, at(24, 10), "approved")
    request(store, "c_no", chat, at(17, 10), "declined")
    request(store, "c_owner", None, at(24, 10))  # his own request: Conker did not ask
    request(store, "c_promise", chat, at(24, 10), "approved", kind="promise")
    request(store, "c_failed", chat, at(24, 10), "failed")
    request(store, "c_keep", chat, at(24, 10), "approved")
    write(
        store,
        "INSERT INTO keep_results(request_id,session_id,kind,target_id,created_at) "
        "VALUES ('c_keep',?,'memory','m1',?)",
        (chat, at(24, 10)),
    )
    proposal(store, "p_yes", at(24, 11), "accepted")
    proposal(store, "p_never", at(24, 11), "never")
    notice(store, "n_plan", "plan", at(24, 12), dismissed=at(24, 13))
    notice(store, "n_job", "agent-job", at(24, 12), session=chat)
    notice(store, "n_recall", "memory-recall", at(24, 12))  # not counted
    approval(store, "turn_1", chat, at(24, 14))
    pushed(store, "ask", "q_open", at(24, 9))
    pushed(store, "approval", "turn_1", at(24, 14))

    page = weeks(store)
    assert page["timeZone"] == ZONE
    assert [week["week"] for week in page["weeks"]] == ["2026-W46", "2026-W47", "2026-W48"]
    first, last, now = page["weeks"]
    assert now["startsAt"] == at(23, 0) and now["endsAt"] == at(30, 0)
    assert first["asks"] == 0 and first["usefulShare"] is None
    assert last["questions"] == {"raised": 1, "actedOn": 1, "dismissed": 0, "unanswered": 0}
    assert last["changeRequests"] == {"raised": 1, "actedOn": 0, "dismissed": 1, "unanswered": 0}
    assert (last["asks"], last["usefulShare"]) == (2, 0.5)
    assert now["questions"] == {"raised": 2, "actedOn": 0, "dismissed": 1, "unanswered": 1}
    assert now["changeRequests"] == {"raised": 1, "actedOn": 1, "dismissed": 0, "unanswered": 0}
    assert now["suggestions"] == {"raised": 2, "actedOn": 1, "dismissed": 1, "unanswered": 0}
    assert now["notices"] == {"raised": 2, "actedOn": 0, "dismissed": 1, "unanswered": 1}
    assert (now["raised"], now["approvalsAsked"], now["asks"], now["pushed"]) == (7, 1, 8, 2)
    assert (now["actedOn"], now["dismissed"], now["unanswered"]) == (2, 3, 2)
    assert now["usefulShare"] == 0.4  # 2 acted on of 5 he could act on


def test_a_trashed_chat_counts_for_nothing_until_restored(store):
    shown = store.create_session(title="Work")
    hidden = store.create_session(title="Private")
    for chat in (shown, hidden):
        ask(store, "q_" + chat, chat, at(24, 9))
        request(store, "c_" + chat, chat, at(24, 9))
        notice(store, "n_" + chat, "agent-job", at(24, 9), session=chat)
        approval(store, "t_" + chat, chat, at(24, 9))
        pushed(store, "ask", "q_" + chat, at(24, 9))
        pushed(store, "approval", "t_" + chat, at(24, 10))
    archive.trash(store, archive.Trash(sessions=[hidden], trashed=True))
    now = weeks(store)["weeks"][-1]
    assert (now["raised"], now["approvalsAsked"], now["pushed"]) == (3, 1, 2)
    archive.trash(store, archive.Trash(sessions=[hidden], trashed=False))
    now = weeks(store)["weeks"][-1]
    assert (now["raised"], now["approvalsAsked"], now["pushed"]) == (6, 2, 4)


def test_an_empty_history_counts_zero_weeks_with_no_share(store):
    page = weeks(store, 8)
    assert len(page["weeks"]) == 8 and page["weeks"][-1]["week"] == "2026-W48"
    for week in page["weeks"]:
        assert (week["asks"], week["pushed"], week["usefulShare"]) == (0, 0, None)
    # Consecutive weeks share their edges, so nothing falls between two of them.
    assert all(
        a["endsAt"] == b["startsAt"] for a, b in zip(page["weeks"], page["weeks"][1:], strict=False)
    )


def test_the_route_is_owner_only_validates_weeks_and_names_its_model(client):
    assert client.get("/insights/quiet/weeks").status_code == 401
    owner = {"X-Pi-Owner-Key": OWNER}
    body = client.get("/insights/quiet/weeks?weeks=4", headers=owner)
    assert body.status_code == 200 and len(body.json()["weeks"]) == 4
    quiet_weeks.QuietWeeks.model_validate(body.json())
    assert len(client.get("/insights/quiet/weeks", headers=owner).json()["weeks"]) == 8
    for count in (0, 27):
        assert client.get(f"/insights/quiet/weeks?weeks={count}", headers=owner).status_code == 422
    assert owner_allowed("GET", "/insights/quiet/weeks")
    assert not owner_allowed("POST", "/insights/quiet/weeks")
    path = client.get("/openapi.json").json()["paths"]["/insights/quiet/weeks"]["get"]
    assert path["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/QuietWeeks"
    }
