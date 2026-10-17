"""Tests for output settings functionality."""

from pathlib import Path

import pytest

from pi.conversation import output_settings
from pi.conversation.store import Store
from pi.turns import prompt_prefix


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
        assert result["output"] == {"format": "auto", "length": "auto", "language": "auto"}

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
        assert result["output"] == {"format": "table", "length": "short", "language": "ru"}

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

        # Test extra key
        with pytest.raises(ValueError):

            class ExtraOutput(output_settings.Output):
                extra: str = "value"

            ExtraOutput(format="auto", length="auto", language="auto")

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
        assert loaded["output"] == {"format": "table", "length": "short", "language": "ru"}

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

        # Should have output in execution
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
        assert exc.value.status == 409

        # Also test forgotten_sessions table
        with store._connect() as db:
            db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            db.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, 1234567890)
            )
            db.execute("INSERT INTO forgotten_sessions (session_id) VALUES (?)", (session_id,))

        with pytest.raises(output_settings.OutputError) as exc:
            output_settings.load(store, session_id)

        assert exc.value.code == "not_found"
        assert exc.value.status == 409

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


class TestHTTPAPI:
    """Test HTTP API endpoints."""

    def test_get_post_endpoints(self):
        """Test GET/POST endpoints work with admin key."""
        from fastapi.testclient import TestClient

        from pi.api.server import app

        # Setup test client (copied from test_owner_context_read.py)
        app.state.store = Store(Path(":memory:"))

        # Create a test session
        with app.state.store._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)", ("test-123", 1234567890)
            )

        client = TestClient(app)

        # Test GET with admin key
        response = client.get(
            "/sessions/test-123/output", headers={"X-Admin-Key": "test-admin-key"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["revision"] == 0
        assert data["output"] == {"format": "auto", "length": "auto", "language": "auto"}

        # Test POST with admin key
        response = client.post(
            "/sessions/test-123/output",
            headers={"X-Admin-Key": "test-admin-key"},
            json={
                "expected_revision": 0,
                "output": {"format": "table", "length": "short", "language": "ru"},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["revision"] == 1
        assert data["output"] == {"format": "table", "length": "short", "language": "ru"}

        # Test GET refused for owner key
        response = client.get(
            "/sessions/test-123/output", headers={"X-Owner-Key": "test-owner-key"}
        )
        assert response.status_code == 401

        # Test GET refused with no key
        response = client.get("/sessions/test-123/output")
        assert response.status_code == 401

        # Test POST refused for owner key
        response = client.post(
            "/sessions/test-123/output",
            headers={"X-Owner-Key": "test-owner-key"},
            json={
                "expected_revision": 1,
                "output": {"format": "list", "length": "medium", "language": "en"},
            },
        )
        assert response.status_code == 401
