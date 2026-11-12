"""One rule says which chats are hidden: forgotten or in the Trash (pi.conversation.visibility)."""

from contextlib import closing

import pytest

from pi.conversation import archive, visibility
from pi.conversation.forgetting import forget, preview
from pi.conversation.store import Store


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def _trash(store, sessions, trashed=True):
    archive.trash(store, archive.Trash(sessions=sessions, trashed=trashed))


def test_forgotten_and_trashed_chats_are_hidden_and_a_restored_one_is_not(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        shown, binned, gone = (store.create_session(title=t) for t in ("shown", "binned", "gone"))
        store.append_message(gone, "user", "secret plan")
        _trash(store, [binned])
    forget(path, gone, preview(path, gone)["confirmation"])
    with closing(Store(path)) as store:
        with store._connect() as db:
            assert visibility.hidden(db, [shown, binned, gone, "", None]) == {binned, gone}
            rows = db.execute(
                f"SELECT id FROM sessions WHERE NOT {visibility.hidden_sql('id')} ORDER BY id"
            ).fetchall()
        assert [row[0] for row in rows] == [shown]
        _trash(store, [binned], trashed=False)
        with store._connect() as db:
            assert visibility.hidden(db, [shown, binned, gone]) == {gone}


def test_kept_things_from_a_hidden_chat_are_named(store):
    kept, binned = (store.create_session(title=t) for t in ("kept", "binned"))
    with store._connect() as db:
        db.executemany(
            "INSERT INTO keep_results(request_id, session_id, kind, target_id, created_at) "
            "VALUES (?,?,?,?,0)",
            [
                ("r1", kept, "memory", "mem_kept"),
                ("r2", binned, "memory", "mem_binned"),
                ("r3", binned, "ai_job", "job_binned"),
            ],
        )
    _trash(store, [binned])
    with store._connect() as db:
        assert visibility.kept_from_hidden(db) == {"mem_binned", "job_binned"}
        assert visibility.kept_from_hidden(db, "memory") == {"mem_binned"}


@pytest.mark.parametrize("column", ["id; DROP TABLE sessions", "s.id x", "", "1id"])
def test_only_a_column_name_is_accepted(column):
    with pytest.raises(ValueError):
        visibility.hidden_sql(column)
