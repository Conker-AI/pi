"""Tests for output settings functionality."""

import pytest

from pi.conversation import output_settings
from pi.conversation.store import Store
from pi.owner import settings as owner_settings
from pi.turns import prompt_prefix

LEVEL_SETTING = output_settings.LEVEL_SETTING


@pytest.fixture
def store(tmp_path):
    """Create a test store."""
    return Store(tmp_path / "test.db")


@pytest.fixture
def session_id():
    """Test session ID."""
    return "test-session-123"


@pytest.fixture
def parent_session_id():
    """Test parent session ID."""
    return "parent-session-456"


class TestOutputSettings:
    """Test output settings load/save functionality."""

    def test_unsaved_chat_defaults(self, store, session_id):
        """Test unsaved chat gives revision 0 and all auto values."""
        # Create a session
        with store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, 1234567890)
            )

        # Load should give defaults
        result = output_settings.load(store, session_id)
        assert result["revision"] == 0
        assert result["output"] == {
            "format": "auto",
            "length": "auto",
            "language": "auto",
            "level": "auto",
        }

        # Snapshot should be None for all auto values
        with store._connect() as db:
            snapshot = output_settings.snapshot(db, session_id)
            assert snapshot is None

        # Messages should be empty for auto values
        messages = output_settings.messages({})
        assert messages == []

    def test_save_load_roundtrip(self, store, session_id):
        """Test save then load round-trips correctly."""
        # Create a session
        with store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, 1234567890)
            )

        # Save custom settings
        update = output_settings.Update(
            expected_revision=0,
            output=output_settings.Output(format="table", length="short", language="ru"),
        )
        result = output_settings.save(store, session_id, update)

        assert result["revision"] == 1
        assert result["output"] == {
            "format": "table",
            "length": "short",
            "language": "ru",
            "level": "auto",
        }

        # Load should return the same
        loaded = output_settings.load(store, session_id)
        assert loaded == result

    def test_revision_conflict(self, store, session_id):
        """Test second save with old revision throws conflict."""
        # Create a session
        with store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, 1234567890)
            )

        # Save first time
        update = output_settings.Update(
            expected_revision=0,
            output=output_settings.Output(format="table", length="short", language="ru"),
        )
        output_settings.save(store, session_id, update)

        # Try to save again with old revision
        with pytest.raises(output_settings.OutputError) as exc:
            output_settings.save(store, session_id, update)

        assert exc.value.code == "revision_conflict"
        assert exc.value.status == 409

    def test_invalid_model_values(self, store, session_id):
        """Test invalid values are refused by the model."""
        # Create a session
        with store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, 1234567890)
            )

        # Test invalid format
        with pytest.raises(ValueError):
            output_settings.Output(format="pdf", length="auto", language="auto")

        # Test invalid length
        with pytest.raises(ValueError):
            output_settings.Output(format="auto", length="huge", language="auto")

        # Test invalid language
        with pytest.raises(ValueError):
            output_settings.Output(format="auto", length="auto", language="English")

        with pytest.raises(ValueError):
            output_settings.Output(format="auto", length="auto", language="r")

        with pytest.raises(ValueError):
            output_settings.Output.model_validate({"format": "auto", "extra": "value"})

        with pytest.raises(ValueError):
            output_settings.Output.model_validate({"language": 7})

        # Test non-string language
        with pytest.raises(ValueError):
            output_settings.Output(format="auto", length="auto", language=123)

    def test_unknown_chat_404(self, store, session_id):
        """Test unknown chat returns 404."""
        # Try to load unknown session
        with pytest.raises(output_settings.OutputError) as exc:
            output_settings.load(store, session_id)

        assert exc.value.code == "not_found"
        assert exc.value.status == 404

        # Try to save to unknown session
        with pytest.raises(output_settings.OutputError) as exc:
            output_settings.save(
                store,
                session_id,
                output_settings.Update(expected_revision=0, output=output_settings.Output()),
            )

        assert exc.value.code == "not_found"
        assert exc.value.status == 404

    def test_branch_inherits_output(self, store, session_id, parent_session_id):
        """Test a branch inherits parent's output settings."""
        # Create parent session with output settings
        with store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)",
                (parent_session_id, 1234567890),
            )
            db.execute(
                "INSERT INTO session_output (session_id, revision, output) VALUES (?, ?, ?)",
                (parent_session_id, 1, '{"format": "table", "length": "short", "language": "ru"}'),
            )

            # Create child session
            db.execute(
                "INSERT INTO sessions (id, parent_id, created_at) VALUES (?, ?, ?)",
                (session_id, parent_session_id, 1234567891),
            )

        # Child should inherit parent's output
        loaded = output_settings.load(store, session_id)
        assert loaded["revision"] == 1
        assert loaded["output"] == {
            "format": "table",
            "length": "short",
            "language": "ru",
            "level": "auto",
        }

    def test_messages_building(self):
        """Test messages builds exactly as specified."""
        # Test with all non-auto values
        execution = {"output": {"format": "table", "length": "short", "language": "ru"}}
        messages = output_settings.messages(execution)

        assert len(messages) == 1
        expected = (
            "Owner output settings for this chat. "
            "Answer format: a table. "
            "Answer length: short, a few sentences. "
            "Answer language: ru, unless the owner asks otherwise."
        )
        assert messages[0].content == expected

        # Test with only format non-auto
        execution = {"output": {"format": "list", "length": "auto", "language": "auto"}}
        messages = output_settings.messages(execution)
        assert len(messages) == 1
        assert messages[0].content == "Owner output settings for this chat. Answer format: a list."

        # Test with all auto returns empty
        execution = {"output": {"format": "auto", "length": "auto", "language": "auto"}}
        messages = output_settings.messages(execution)
        assert messages == []

        # Test with None returns empty
        messages = output_settings.messages({})
        assert messages == []

    @pytest.mark.parametrize("level", ["strict", "plain", "deep"])
    def test_saved_level_reaches_snapshot_and_prompt(self, store, session_id, level):
        """A level saved on the chat lands in the snapshot and teaches the model its rule."""
        # Create a session
        with store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, 1234567890)
            )

        update = output_settings.Update(
            expected_revision=0, output=output_settings.Output(level=level)
        )
        result = output_settings.save(store, session_id, update)
        assert result["output"]["level"] == level

        loaded = output_settings.load(store, session_id)
        assert loaded["output"]["level"] == level

        with store._connect() as db:
            snapshot = output_settings.snapshot(db, session_id)
            assert snapshot["level"] == level

        execution = {"output": snapshot}
        messages = output_settings.messages(execution)
        assert len(messages) == 1
        expected = (
            f"Owner output settings for this chat. Language level: {output_settings.LEVELS[level]}."
        )
        assert messages[0].content == expected

        prefix = prompt_prefix.system_messages(store, "base prompt", execution, None, None)
        taught = [m for m in prefix if "Owner output settings" in m.content]
        assert len(taught) == 1
        assert f"Language level: {output_settings.LEVELS[level]}" in taught[0].content

    def test_normal_level_adds_nothing(self, store, session_id):
        """Normal saved on the chat is no rule at all: the snapshot stays None when all else Auto."""
        # Create a session
        with store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, 1234567890)
            )

        output_settings.save(
            store,
            session_id,
            output_settings.Update(
                expected_revision=0, output=output_settings.Output(level="normal")
            ),
        )
        assert output_settings.load(store, session_id)["output"]["level"] == "normal"

        with store._connect() as db:
            assert output_settings.snapshot(db, session_id) is None

        assert output_settings.messages({"output": {"level": "normal"}}) == []

    def test_invalid_level_refused(self, store, session_id):
        """A value outside the level list is refused by the model."""
        with pytest.raises(ValueError):
            output_settings.Output(format="auto", length="auto", language="auto", level="simple")

        with pytest.raises(ValueError):
            output_settings.Update.model_validate(
                {"expected_revision": 0, "output": {"level": "simple"}}
            )

    def test_owner_default_language_level(self, store, session_id):
        """Level Auto follows the owner's chats.language_level; a saved level wins over it."""
        # Create a session
        with store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, 1234567890)
            )

        revision = owner_settings.load(store)["revision"]

        def choose(value):
            nonlocal revision
            owner_settings.save(
                store,
                owner_settings.Update(expected_revision=revision, values={LEVEL_SETTING: value}),
            )
            revision += 1

        # No owner setting and Auto on the chat: Normal, nothing added.
        with store._connect() as db:
            assert output_settings.snapshot(db, session_id) is None

        # Owner default deep, chat Auto: the snapshot resolves to deep.
        choose("deep")
        with store._connect() as db:
            assert output_settings.snapshot(db, session_id) == {
                **output_settings.AUTO,
                "level": "deep",
            }

        # A level saved on the chat wins over the owner's default.
        output_settings.save(
            store,
            session_id,
            output_settings.Update(
                expected_revision=0, output=output_settings.Output(level="plain")
            ),
        )
        with store._connect() as db:
            snapshot = output_settings.snapshot(db, session_id)
            assert snapshot["level"] == "plain"
            assert snapshot["format"] == "auto" and snapshot["language"] == "auto"

        # Back to Auto on the chat, an unknown owner value means Normal: nothing added.
        output_settings.save(
            store,
            session_id,
            output_settings.Update(expected_revision=1, output=output_settings.Output()),
        )
        choose("bogus")
        with store._connect() as db:
            assert output_settings.snapshot(db, session_id) is None

    def test_old_row_without_level_loads_as_auto(self, store, session_id):
        """A row saved before the level existed loads with level Auto."""
        # Create a session
        with store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, 1234567890)
            )
            db.execute(
                "INSERT INTO session_output (session_id, revision, output) VALUES (?, ?, ?)",
                (session_id, 3, '{"format": "table", "length": "short", "language": "ru"}'),
            )

        loaded = output_settings.load(store, session_id)
        assert loaded["revision"] == 3
        assert loaded["output"] == {
            "format": "table",
            "length": "short",
            "language": "ru",
            "level": "auto",
        }

    def test_turn_snapshot_includes_output(self, store, session_id):
        """Test the turn snapshot carries output settings."""
        # Create a session
        with store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, 1234567890)
            )

        # Save output settings
        update = output_settings.Update(
            expected_revision=0,
            output=output_settings.Output(format="table", length="short", language="ru"),
        )
        output_settings.save(store, session_id, update)

        # Get execution snapshot
        from pi.conversation import settings as session_settings

        execution = session_settings.execution(store, session_id)

        # Should have output in execution; level auto resolves to Normal, which adds no key
        assert execution["output"] == {"format": "table", "length": "short", "language": "ru"}

        # Test with all auto - should be None
        with store._connect() as db:
            db.execute("DELETE FROM session_output WHERE session_id = ?", (session_id,))

        execution = session_settings.execution(store, session_id)
        assert execution.get("output") is None

    def test_system_prefix_includes_output(self, store, session_id):
        """Test the system prefix includes output message."""
        # Create a session
        with store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, 1234567890)
            )

        # Test with output settings
        execution = {"output": {"format": "table", "length": "short", "language": "ru"}}
        messages = prompt_prefix.system_messages(store, "base prompt", execution, None, None)

        # Find the output message
        output_messages = [m for m in messages if "Owner output settings" in m.content]
        assert len(output_messages) == 1
        assert "Answer format: a table" in output_messages[0].content

        # Test with no output - should be absent
        execution = {}
        messages = prompt_prefix.system_messages(store, "base prompt", execution, None, None)
        output_messages = [m for m in messages if "Owner output settings" in m.content]
        assert len(output_messages) == 0

    def test_forgotten_session_404(self, store, session_id):
        """Test forgotten sessions return 404."""
        # Create a forgotten session
        with store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, status, created_at) VALUES (?, ?, ?)",
                (session_id, "forgotten", 1234567890),
            )

        # Should return 404
        with pytest.raises(output_settings.OutputError) as exc:
            output_settings.load(store, session_id)

        assert exc.value.code == "not_found"
        assert exc.value.status == 404

    def test_closed_session_error(self, store, session_id):
        """Test closed sessions can't be modified."""
        # Create a closed session
        with store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, status, created_at) VALUES (?, ?, ?)",
                (session_id, "closed", 1234567890),
            )

        # Should return session_closed error
        with pytest.raises(output_settings.OutputError) as exc:
            output_settings.save(
                store,
                session_id,
                output_settings.Update(expected_revision=0, output=output_settings.Output()),
            )

        assert exc.value.code == "session_closed"
        assert exc.value.status == 409


OWNER = "output_settings_owner_key"


@pytest.fixture
def http(tmp_path, monkeypatch):
    import hashlib
    from contextlib import closing

    from fastapi.testclient import TestClient

    from pi.api import server as api

    with closing(Store(tmp_path / "pi.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
        monkeypatch.setattr(
            api.app.state,
            "owner_key_hash",
            hashlib.sha256(OWNER.encode()).hexdigest(),
            raising=False,
        )
        yield store, TestClient(api.app)


def test_routes_accept_the_admin_and_owner_keys(http):
    store, client = http
    session = store.create_session()
    admin = {"X-Pi-Key": "synthetic-admin"}
    response = client.get(f"/sessions/{session}/output", headers=admin)
    assert response.status_code == 200
    assert response.json() == {"revision": 0, "output": output_settings.AUTO_WITH_LEVEL}
    body = {
        "expected_revision": 0,
        "output": {"format": "table", "length": "short", "language": "ru"},
    }
    response = client.post(f"/sessions/{session}/output", headers=admin, json=body)
    assert response.status_code == 200
    assert response.json() == {"revision": 1, "output": {**body["output"], "level": "auto"}}
    stale = client.post(f"/sessions/{session}/output", headers=admin, json=body)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "revision_conflict"
    assert client.get("/sessions/ses_missing/output", headers=admin).status_code == 404
    owner = {"X-Pi-Owner-Key": OWNER}
    assert client.get(f"/sessions/{session}/output", headers=owner).status_code == 200
    again = {**body, "expected_revision": 1}
    assert client.post(f"/sessions/{session}/output", headers=owner, json=again).status_code == 200
    assert client.get("/sessions/ses_missing/output", headers=owner).status_code == 403
    assert client.get(f"/sessions/{session}/output").status_code == 401
