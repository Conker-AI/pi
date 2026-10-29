"""Tests for output settings functionality."""

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

        # The snapshot carries only the owner's interactive answers choice (Auto by default)
        with store._connect() as db:
            snapshot = output_settings.snapshot(db, session_id)
            assert snapshot == {**output_settings.AUTO, "interactive": "auto"}
            db.execute(
                "UPDATE owner_settings SET vals=? WHERE singleton=1",
                ('{"chats.interactive_answers": "off"}',),
            )
            assert output_settings.snapshot(db, session_id) is None

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

        # Should have output in execution, with the owner's interactive answers choice
        assert execution["output"] == {
            "format": "table",
            "length": "short",
            "language": "ru",
            "interactive": "auto",
        }

        # Test with all auto and interactive answers off - should be None
        with store._connect() as db:
            db.execute("DELETE FROM session_output WHERE session_id = ?", (session_id,))
            db.execute(
                "UPDATE owner_settings SET vals=? WHERE singleton=1",
                ('{"chats.interactive_answers": "off"}',),
            )

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


def test_routes_accept_only_the_admin_key(http):
    store, client = http
    session = store.create_session()
    admin = {"X-Pi-Key": "synthetic-admin"}
    response = client.get(f"/sessions/{session}/output", headers=admin)
    assert response.status_code == 200
    assert response.json() == {"revision": 0, "output": output_settings.AUTO}
    body = {
        "expected_revision": 0,
        "output": {"format": "table", "length": "short", "language": "ru"},
    }
    response = client.post(f"/sessions/{session}/output", headers=admin, json=body)
    assert response.status_code == 200
    assert response.json() == {"revision": 1, "output": body["output"]}
    stale = client.post(f"/sessions/{session}/output", headers=admin, json=body)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "revision_conflict"
    assert client.get("/sessions/ses_missing/output", headers=admin).status_code == 404
    owner = {"X-Pi-Owner-Key": OWNER}
    assert client.get(f"/sessions/{session}/output", headers=owner).status_code == 401
    assert client.post(f"/sessions/{session}/output", headers=owner, json=body).status_code == 401
    assert client.get(f"/sessions/{session}/output").status_code == 401
