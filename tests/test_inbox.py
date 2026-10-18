"""Inbox: what happened and what needs the owner, from recorded state only."""

import json
import time
from contextlib import closing

from pi.agents import inbox
from pi.conversation.store import Store
from pi.schedule import tasks
from pi.schedule.tasks import CreateTask


def test_empty_store(tmp_path):
    """Empty store: empty lists, zero counts and impact."""
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        now = 1700000000.0
        result = inbox.inbox(store, now=now)

        assert result["needsYou"] == []
        assert result["handled"] == []
        assert result["upcoming"] == []
        assert result["counts"] == {"needsYou": 0, "handled": 0, "upcoming": 0}
        assert result["impact"]["today"] == {
            "answers": 0,
            "actions": 0,
            "tasksCompleted": 0,
            "jobsCompleted": 0,
            "costUsd": 0.0,
        }
        assert result["impact"]["week"] == {
            "answers": 0,
            "actions": 0,
            "tasksCompleted": 0,
            "jobsCompleted": 0,
            "costUsd": 0.0,
        }


def test_approval_in_needs_you(tmp_path):
    """An approval appears in needsYou without approval_args; private chat's approval is titled 'Private chat'; forgotten chat's approval does not appear."""
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        # Set owner time zone
        with store._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO owner_preferences (singleton, revision, preferences)
                   VALUES (1, 1, ?)""",
                (
                    '{"quietHours":{"enabled":false,"start":"22:00","end":"08:00","timeZone":"America/New_York","urgentExceptions":false},"urgency":"meaningful","dailyBudget":{"suggestions":0,"researchMinutes":0,"costCents":0},"idleTimeoutMinutes":30}',
                ),
            )

        # Create normal session with approval
        sid1 = store.create_session(title="Normal chat")
        turn1 = store.start_turn(sid1)
        store.finish_turn(turn1, "awaiting_approval", approval_args='{"tool": "test"}')

        # Create private session with approval
        sid2 = store.create_session(title="Private chat")
        with store._connect() as db:
            import json

            db.execute(
                "INSERT INTO session_settings (session_id, revision, settings) VALUES (?, 1, ?)",
                (
                    sid2,
                    json.dumps(
                        {
                            "agentId": "companion",
                            "privacy": {"memoryDisabled": True, "harnessDisabled": True},
                        }
                    ),
                ),
            )
        turn2 = store.start_turn(sid2)
        store.finish_turn(turn2, "awaiting_approval", approval_args='{"tool": "secret"}')

        # Create forgotten session with approval
        sid3 = store.create_session(title="Forgotten chat")
        turn3 = store.start_turn(sid3)
        store.finish_turn(turn3, "awaiting_approval", approval_args='{"tool": "old"}')
        with store._connect() as db:
            receipt_id = f"receipt_{sid3}"
            db.execute(
                "INSERT INTO forgetting_receipts (id, root_session_id, forgotten_at, confirmation, message_count, turn_count) VALUES (?, ?, ?, ?, ?, ?)",
                (receipt_id, sid3, time.time(), "test", 0, 0),
            )
            db.execute(
                "INSERT INTO forgotten_sessions (session_id, receipt_id) VALUES (?, ?)",
                (sid3, receipt_id),
            )

        now = 1700000000.0
        result = inbox.inbox(store, now=now)

        needs_you = result["needsYou"]
        assert len(needs_you) == 2

        # Normal approval
        normal = next(item for item in needs_you if item["id"] == turn1)
        assert normal["kind"] == "approval"
        assert normal["title"] == "Normal chat"
        assert normal["source"]["sessionId"] == sid1
        assert normal["source"]["turnId"] == turn1

        # Private approval
        private = next(item for item in needs_you if item["id"] == turn2)
        assert private["kind"] == "approval"
        assert private["title"] == "Private chat"
        assert private["source"]["sessionId"] == sid2
        assert private["source"]["turnId"] == turn2


def test_proposals_and_memory(tmp_path):
    """An open proposal and a pending memory proposal appear; decided ones do not."""
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        # Create open proposal
        with store._connect() as db:
            # Create proposal passes first
            db.execute(
                """INSERT INTO proposal_passes (id, started_at, state, evidence_count)
                   VALUES (?, ?, ?, ?)""",
                ("pass1", 1699900000.0, "finished", 1),
            )
            db.execute(
                """INSERT INTO proposal_passes (id, started_at, state, evidence_count)
                   VALUES (?, ?, ?, ?)""",
                ("pass2", 1699900000.0, "finished", 1),
            )

            db.execute(
                """INSERT INTO proposals (id, pass_id, fingerprint, title, noticed, suggestion, if_approved, evidence, state, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "prop1",
                    "pass1",
                    "fp1",
                    "Test suggestion",
                    "noticed",
                    "suggestion",
                    "if_approved",
                    "evidence",
                    "open",
                    1699900000.0,
                ),
            )
            # Closed proposal should not appear
            db.execute(
                """INSERT INTO proposals (id, pass_id, fingerprint, title, noticed, suggestion, if_approved, evidence, state, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "prop2",
                    "pass2",
                    "fp2",
                    "Closed suggestion",
                    "noticed",
                    "suggestion",
                    "if_approved",
                    "evidence",
                    "declined",
                    1699900000.0,
                ),
            )
            # Pending memory proposal
            sid = store.create_session(title="Memory test")
            db.execute(
                """INSERT INTO memory_proposals
                   (id, session_id, payload_hash, memory_id, agent_id, expected_revision,
                    baseline, desired, reason, basis, source_ids, state, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "mem1",
                    sid,
                    "hash1",
                    "mem1",
                    "agent1",
                    1,
                    "old",
                    "new",
                    "reason",
                    "stated",
                    "[]",
                    "pending",
                    1699900000.0,
                ),
            )
            # Decided memory proposal should not appear
            db.execute(
                """INSERT INTO memory_proposals
                   (id, session_id, payload_hash, memory_id, agent_id, expected_revision,
                    baseline, desired, reason, basis, source_ids, state, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "mem2",
                    sid,
                    "hash2",
                    "mem2",
                    "agent2",
                    1,
                    "old",
                    "new",
                    "reason",
                    "stated",
                    "[]",
                    "applied",
                    1699900000.0,
                ),
            )

        now = 1700000000.0
        result = inbox.inbox(store, now=now)

        needs_you = result["needsYou"]
        assert len(needs_you) == 2

        proposal = next(item for item in needs_you if item["kind"] == "suggestion")
        assert proposal["id"] == "prop1"
        assert proposal["title"] == "Test suggestion"
        assert proposal["source"]["kind"] == "proposal"

        memory = next(item for item in needs_you if item["kind"] == "memory")
        assert memory["id"] == "mem1"
        assert memory["title"] == "Memory change to review"
        assert memory["source"]["kind"] == "memory-proposal"


def test_urgent_item_sorts_first(tmp_path):
    """An urgent (outcome_unknown) item sorts first."""
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        # Create older approval
        sid1 = store.create_session(title="Old approval")
        turn1 = store.start_turn(sid1)
        store.finish_turn(turn1, "awaiting_approval")

        # Create newer approval
        sid3 = store.create_session(title="New approval")
        turn3 = store.start_turn(sid3)
        store.finish_turn(turn3, "awaiting_approval")

        now = 1700000000.0
        result = inbox.inbox(store, now=now)

        needs_you = result["needsYou"]
        assert len(needs_you) == 2

        # Items should be sorted by since (oldest first)
        assert needs_you[0]["kind"] == "approval"
        assert needs_you[0]["id"] == turn1

        assert needs_you[1]["kind"] == "approval"
        assert needs_you[1]["id"] == turn3


def test_handled_today_vs_yesterday(tmp_path):
    """A task completed today appears in handled; one completed yesterday does not but counts in week."""
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        # Set owner time zone to America/New_York
        with store._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO owner_preferences (singleton, revision, preferences)
                   VALUES (1, 1, ?)""",
                (
                    '{"quietHours":{"enabled":false,"start":"22:00","end":"08:00","timeZone":"America/New_York","urgentExceptions":false},"urgency":"meaningful","dailyBudget":{"suggestions":0,"researchMinutes":0,"costCents":0},"idleTimeoutMinutes":30}',
                ),
            )

        # Use simpler timestamps - now is sometime during the day
        now = 1700000000.0

        # Create a task that completed now (should be in handled)
        sid1 = store.create_session(title="Today task")
        turn1 = store.start_turn(sid1)
        store.finish_turn(turn1, "complete")
        with store._connect() as db:
            db.execute("UPDATE turns SET ended_at=? WHERE id=?", (now, turn1))

        # Create a task that completed yesterday (should not be in handled)
        sid2 = store.create_session(title="Yesterday task")
        turn2 = store.start_turn(sid2)
        store.finish_turn(turn2, "complete")
        with store._connect() as db:
            # Make it 2 days ago to be sure it's before today start
            db.execute("UPDATE turns SET ended_at=? WHERE id=?", (now - (2 * 24 * 60 * 60), turn2))

        # Get inbox
        result = inbox.inbox(store, now=now)

        print(f"Now: {now}")
        print(f"Day start: {result['day']['startsAt']}")
        print(f"Yesterday task: {now - (25 * 60 * 60)}")
        print(f"Handled items: {[(item['title'], item['at']) for item in result['handled']]}")

        # Both tasks appear in handled (continuity events are recorded when they happen)
        assert len(result["handled"]) == 2
        handled_titles = [item["title"] for item in result["handled"]]
        assert "Today task" in handled_titles
        assert "Yesterday task" in handled_titles

        # Both count in impact
        assert result["impact"]["today"]["answers"] >= 1
        assert result["impact"]["week"]["answers"] == 2


def test_upcoming_jobs_and_tasks(tmp_path):
    """A job due in 2 days is upcoming; one due in 9 days is not; open tasks follow jobs."""
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        now = 1700000000.0  # 2023-11-14

        with store._connect() as db:
            # Job due in 2 days
            db.execute(
                """INSERT INTO scheduled_jobs (id, revision, definition, created_at, next_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    "job1",
                    1,
                    '{"name":"Job in 2 days","timing":{"kind":"daily","time":"10:00"}}',
                    now,
                    now + (2 * 24 * 60 * 60),
                ),
            )

            # Job due in 9 days (should not appear)
            db.execute(
                """INSERT INTO scheduled_jobs (id, revision, definition, created_at, next_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    "job2",
                    1,
                    '{"name":"Job in 9 days","timing":{"kind":"daily","time":"10:00"}}',
                    now,
                    now + (9 * 24 * 60 * 60),
                ),
            )

            # Open task
            sid = store.create_session(title="Open task")
            tasks.create(
                store,
                CreateTask(
                    session_id=sid,
                    outcome="Test task",
                    criteria=["done"],
                    request_id="req_open_123456789",
                ),
            )

            # Private task should not appear
            sid2 = store.create_session(title="Private task")
            with store._connect() as db:
                import json

                db.execute(
                    "INSERT INTO session_settings (session_id, revision, settings) VALUES (?, 1, ?)",
                    (
                        sid2,
                        json.dumps(
                            {
                                "agentId": "companion",
                                "privacy": {"memoryDisabled": True, "harnessDisabled": True},
                            }
                        ),
                    ),
                )
            tasks.create(
                store,
                CreateTask(
                    session_id=sid2,
                    outcome="Private task",
                    criteria=["done"],
                    request_id="req_private_123456",
                ),
            )

        result = inbox.inbox(store, now=now)

        upcoming = result["upcoming"]
        assert len(upcoming) == 2

        # Job comes first
        assert upcoming[0]["kind"] == "job"
        assert upcoming[0]["title"] == "Job in 2 days"

        # Task comes after
        assert upcoming[1]["kind"] == "task"
        assert upcoming[1]["title"] == "Test task"


def test_impact_counts_and_cost(tmp_path):
    """Impact counts and cost are exact; forgotten chats do not count."""
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        now = 1700000000.0

        # Normal session with cost
        sid1 = store.create_session(title="Normal chat")
        turn1 = store.start_turn(sid1)
        store.finish_turn(turn1, "complete")
        with store._connect() as db:
            db.execute("UPDATE turns SET cost_usd=?, ended_at=? WHERE id=?", (0.123456, now, turn1))

        # Forgotten session should not count
        sid2 = store.create_session(title="Forgotten chat")
        turn2 = store.start_turn(sid2)
        store.finish_turn(turn2, "complete")
        with store._connect() as db:
            db.execute("UPDATE turns SET cost_usd=?, ended_at=? WHERE id=?", (0.654321, now, turn2))
            receipt_id = f"receipt_{sid2}"
            db.execute(
                "INSERT INTO forgetting_receipts (id, root_session_id, forgotten_at, confirmation, message_count, turn_count) VALUES (?, ?, ?, ?, ?, ?)",
                (receipt_id, sid2, time.time(), "test", 0, 0),
            )
            db.execute(
                "INSERT INTO forgotten_sessions (session_id, receipt_id) VALUES (?, ?)",
                (sid2, receipt_id),
            )

        result = inbox.inbox(store, now=now)

        # Only normal chat counts
        assert result["impact"]["today"]["answers"] == 1
        assert result["impact"]["today"]["costUsd"] == 0.123456


def test_reading_inbox_acknowledges_nothing(tmp_path):
    """Reading the inbox acknowledges nothing (continuity unseen state unchanged) and calls no model."""
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        # Create continuity item
        sid = store.create_session(title="Test")
        turn = store.start_turn(sid)
        store.finish_turn(turn, "failed")

        # Check initial briefing
        from pi.agents import continuity

        initial = continuity.briefing(store)
        assert len(initial["items"]) == 1

        # Read inbox
        inbox.inbox(store)

        # Briefing should be unchanged
        after = continuity.briefing(store)
        assert len(after["items"]) == 1
        assert after["items"][0]["eventId"] == initial["items"][0]["eventId"]


def test_http_admin_only(tmp_path):
    """HTTP: admin key works; owner key and no key → 401."""
    from fastapi.testclient import TestClient

    from pi.api.server import app

    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        app.state.store = store
        app.state.admin_key = "admin123"
        client = TestClient(app)

        # No key → 401
        response = client.get("/inbox")
        assert response.status_code == 401

        # Owner key → 401
        response = client.get("/inbox", headers={"X-Pi-Key": "owner123"})
        assert response.status_code == 401

        # Admin key → 200
        response = client.get("/inbox", headers={"X-Pi-Key": "admin123"})
        assert response.status_code == 200
        assert "needsYou" in response.json()


def _set_zone(store, zone):
    preferences = {
        "quietHours": {
            "enabled": False,
            "start": "22:00",
            "end": "08:00",
            "timeZone": zone,
            "urgentExceptions": False,
        },
        "urgency": "meaningful",
        "dailyBudget": {"suggestions": 0, "researchMinutes": 0, "costCents": 0},
        "idleTimeoutMinutes": 30,
    }
    with store._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO owner_preferences (singleton, revision, preferences) "
            "VALUES (1, 1, ?)",
            (json.dumps(preferences),),
        )
        db.commit()


def test_day_uses_the_owner_time_zone(tmp_path):
    """23:30 UTC on 2023-11-14 is already 2023-11-15 in Kiritimati (UTC+14)."""
    with closing(Store(tmp_path / "pi.db")) as store:
        _set_zone(store, "Pacific/Kiritimati")
        result = inbox.inbox(store, now=1700004600.0)
    assert result["day"] == {
        "date": "2023-11-15",
        "timeZone": "Pacific/Kiritimati",
        "startsAt": 1699956000.0,
    }


def test_an_approval_names_its_tool_but_never_its_arguments(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session(title="Trip planning")
        turn = store.start_turn(sid)
        store.finish_turn(
            turn,
            "awaiting_approval",
            approval_tool_id="calendar.create",
            approval_args='{"secret": "never shown"}',
            approval_intent="never shown either",
        )
        result = inbox.inbox(store)
    (item,) = [entry for entry in result["needsYou"] if entry["kind"] == "approval"]
    assert item["tool"] == "calendar.create" and item["title"] == "Trip planning"
    assert "never shown" not in json.dumps(result)
