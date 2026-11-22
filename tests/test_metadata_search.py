"""Global metadata search bounds authorized matches, not recent candidates."""

import json
from contextlib import closing, contextmanager

import pytest
from test_universal_search import client_for

from pi.context import universal_search as search
from pi.conversation import forgetting
from pi.conversation import settings as session_settings
from pi.conversation.store import Store


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "search.db")) as value:
        yield value


def populate(store, count, *, prefix="ses_", title="Needle", created_at=1, private=False):
    ids = [f"{prefix}{index:04}" for index in range(count)]
    with store._connect() as db:
        db.executemany(
            "INSERT INTO sessions(id,title,created_at) VALUES(?,?,?)",
            [(identity, title, created_at) for identity in ids],
        )
        if private:
            settings = json.dumps(
                {
                    "agentId": "companion",
                    "privacy": {"memoryDisabled": True, "harnessDisabled": False},
                }
            )
            db.executemany(
                "INSERT INTO session_settings VALUES(?,1,?)",
                [(identity, settings) for identity in ids],
            )
    return ids


def metadata(store, query="needle"):
    return search.lexical(store, None, None, query, {"sources": ["conversations"]}, "metadata")


def test_older_titles_and_full_summaries_match_before_recent_limit(store):
    populate(store, 250, title="Unrelated", created_at=2)
    title_id = store.create_session(title="Old unique title")
    summary_id = store.create_session(title="Other", summary="x" * 30_000 + "Old unique summary")
    with store._connect() as db:
        db.execute("UPDATE sessions SET created_at=0 WHERE id IN (?,?)", (title_id, summary_id))
    assert {title_id, summary_id}.isdisjoint(row["id"] for row in store.list_sessions(200))
    rows, coverage = metadata(store, "OLD UNIQUE")
    assert {row["recordId"] for row in rows} == {title_id, summary_id}
    assert coverage == [{"source": "conversations", "status": "searched"}]
    summary = next(row for row in rows if row["recordId"] == summary_id)
    assert "Old unique summary" in summary["excerpt"] and len(summary["excerpt"]) <= 400
    assert all(row["matchType"] == "metadata" and row["role"] is None for row in rows)


def test_absent_query_in_large_corpus_is_fully_searched(store):
    populate(store, 350)
    assert metadata(store, "not retained anywhere") == (
        [],
        [{"source": "conversations", "status": "searched"}],
    )


@pytest.mark.parametrize("count", [0, 199, 200, 201, 250])
def test_coverage_reflects_eligible_match_overflow_and_tied_order(store, count):
    ids = populate(store, count)
    rows, coverage = metadata(store)
    assert [row["recordId"] for row in rows] == list(reversed(ids))[:200]
    assert coverage == [
        {"source": "conversations", "status": "partial" if count > 200 else "searched"}
    ]


def test_private_matches_do_not_consume_public_result_budget(store):
    ids = populate(store, 3, prefix="ses_public_", created_at=0)
    populate(store, 250, prefix="ses_private_", created_at=2, private=True)
    populate(store, 250, prefix="ses_unrelated_", title="Other", created_at=3)
    rows, coverage = metadata(store)
    assert [row["recordId"] for row in rows] == list(reversed(ids))
    assert coverage == [{"source": "conversations", "status": "searched"}]


@pytest.mark.parametrize("key", ["memoryDisabled", "harnessDisabled"])
def test_current_inherited_and_immutable_historical_privacy_are_preserved(store, key):
    public = store.create_session(title="Needle public")
    current = store.create_session(title="Needle current", privacy={key: True})
    child = store.create_session(title="Needle child", parent_id=current)
    historical = store.create_session(title="Needle historical", privacy={key: True})
    store.append_message(historical, "user", "Original private input")
    setting = session_settings.load(store, historical)
    setting["settings"]["privacy"][key] = False
    session_settings.save(
        store,
        historical,
        session_settings.Update(
            expected_revision=setting["revision"],
            settings=session_settings.Settings.model_validate(setting["settings"]),
        ),
    )
    rows, coverage = metadata(store)
    assert [row["recordId"] for row in rows] == [public]
    assert {current, child, historical}.isdisjoint(row["recordId"] for row in rows)
    assert coverage == [{"source": "conversations", "status": "searched"}]


def test_legacy_memory_skip_default_and_explicit_public_override_are_preserved(store):
    legacy = populate(store, 1)[0]  # No settings row: the shared default is authoritative.
    explicit = store.create_session(
        title="Needle explicit", privacy={"memoryDisabled": False, "harnessDisabled": False}
    )
    with store._connect() as db:
        db.execute("INSERT INTO setup_optional_choices VALUES('memory',1,'choice_skip','skip',0)")
    rows, _ = metadata(store)
    assert [row["recordId"] for row in rows] == [explicit]
    with store._connect() as db:
        db.execute(
            "INSERT INTO setup_optional_choices VALUES('memory',2,'choice_include','include',1)"
        )
    rows, _ = metadata(store)
    assert {row["recordId"] for row in rows} == {legacy, explicit}


@pytest.mark.parametrize(
    "title,summary,query",
    [
        ("ПРИВЕТ", "", "привет"),
        ("Straße", "", "STRASSE"),
        ("Other", "שלום", "שלום"),
        ("İstanbul", "", "i\u0307STANBUL"),
        ("100%_literal\\path", "", "%_literal\\"),
        ("Quote", "' OR 1=1 --", "' OR 1=1 --"),
        ("Title", "Summary", "title\nsummary"),
        ("Title", "A\x00B", "a\x00b"),
    ],
)
def test_matching_remains_literal_unicode_and_uses_original_metadata_haystack(
    store, title, summary, query
):
    sid = store.create_session(title=title, summary=summary)
    store.create_session(title="Unrelated 1000 literal path")
    rows, _ = metadata(store, query)
    assert rows == [
        search.record("conversations", sid, title, summary, f"/chat?session={sid}", query)
    ]


def test_matches_beyond_preview_limits_remain_bounded_results(store):
    title = "x" * 5000 + " needle"
    sid = store.create_session(title=title, summary="界" * 10000)
    rows, _ = metadata(store)
    assert len(rows) == 1 and rows[0]["recordId"] == sid
    assert rows[0]["title"] == title[:500]
    assert len(rows[0]["excerpt"]) == 400
    assert set(rows[0]) == {
        "id",
        "source",
        "recordId",
        "title",
        "excerpt",
        "href",
        "matchType",
        "role",
    }


def test_rows_stream_and_stop_at_201_eligible_hits_with_no_writes(store, monkeypatch):
    populate(store, 1000)
    connect, policy = store._connect, session_settings.source_privacy
    calls, connections, statements = [], [], []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __iter__(self):
            return iter(self.cursor)

        def fetchall(self):
            pytest.fail("Metadata matches must be streamed, not materialized before the cap")

    class Connection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, statement, *args):
            cursor = self.db.execute(statement, *args)
            return Cursor(cursor) if statement.startswith("SELECT id,title,summary") else cursor

    @contextmanager
    def hooked():
        with connect() as db:
            db.set_trace_callback(statements.append)
            connection = Connection(db)
            connections.append(connection)
            yield connection

    def checked(db, identity):
        assert db is connections[-1]
        calls.append(identity)
        return policy(db, identity)

    monkeypatch.setattr(store, "_connect", hooked)
    monkeypatch.setattr(session_settings, "source_privacy", checked)
    with connect() as monitor:
        version = monitor.execute("PRAGMA data_version").fetchone()[0]
        rows, coverage = metadata(store)
        assert monitor.execute("PRAGMA data_version").fetchone()[0] == version
    assert len(rows) == 200 and len(calls) == 201 and len(connections) == 1
    assert coverage == [{"source": "conversations", "status": "partial"}]
    assert "BEGIN" in statements
    assert not any("m.content" in statement for statement in statements)


def test_matching_and_privacy_share_one_read_snapshot_during_concurrent_change(store, monkeypatch):
    sid = store.create_session(
        title="Needle old", privacy={"memoryDisabled": False, "harnessDisabled": False}
    )
    connect = store._connect
    changed = False

    class Connection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, statement, *args):
            nonlocal changed
            cursor = self.db.execute(statement, *args)
            if statement.startswith("SELECT id,title,summary") and not changed:
                changed = True
                with connect() as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    writer.execute("UPDATE sessions SET title='Needle new' WHERE id=?", (sid,))
                    writer.execute(
                        "UPDATE session_settings SET settings=json_set(settings,'$.privacy.memoryDisabled',1) WHERE session_id=?",
                        (sid,),
                    )
                    writer.commit()
            return cursor

    @contextmanager
    def hooked():
        with connect() as db:
            yield Connection(db)

    monkeypatch.setattr(store, "_connect", hooked)
    rows, _ = metadata(store)
    assert len(rows) == 1 and rows[0]["title"] == "Needle old"
    assert metadata(store)[0] == []


def test_existing_route_pagination_and_exact_message_search_are_unchanged(store):
    populate(store, 207)
    message = store.append_message("ses_0000", "user", "needle in an older retained message")
    search.save_settings(
        store,
        search.SaveSettings(
            expected_revision=0, configuration=search.Settings(sources=["conversations"])
        ),
        semantic_available=False,
        ranking_available=False,
    )
    client = client_for(store)
    params, records = {"q": "needle", "stage": "metadata", "limit": 50}, []
    while True:
        response = client.get("/search", params=params)
        assert response.status_code == 200
        page = response.json()
        assert page["coverage"] == [{"source": "conversations", "status": "partial"}]
        assert page["ranking"] == {"status": "disabled"} and page["stage"] == "metadata"
        records.extend(page["results"])
        if page["nextCursor"] is None:
            break
        params["cursor"] = page["nextCursor"]
    assert len(records) == 200 and len({row["id"] for row in records}) == 200
    assert [row["recordId"] for row in records] == [f"ses_{i:04}" for i in range(7, 207)]
    exact = client.get("/search", params={"q": "needle", "stage": "text"}).json()
    assert [row["recordId"] for row in exact["results"]] == [message["id"]]
    assert exact["coverage"] == [{"source": "conversations", "status": "searched"}]


def test_forgotten_conversation_never_contributes_metadata(tmp_path):
    path = tmp_path / "forget.db"
    with closing(Store(path)) as store:
        sid = store.create_session(title="Needle title", summary="Needle summary")
    forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])
    with closing(Store(path)) as store:
        assert metadata(store) == ([], [{"source": "conversations", "status": "searched"}])


def test_forgotten_ledger_masks_inconsistent_legacy_open_metadata(store):
    public = populate(store, 1, prefix="ses_public_", created_at=0)[0]
    forgotten = populate(store, 201, prefix="ses_forgotten_", created_at=2)
    with store._connect() as db:
        db.executemany(
            "INSERT INTO forgetting_receipts VALUES(?,?,0,'confirmed',0,0)",
            [(f"del_{sid}", sid) for sid in forgotten],
        )
        db.executemany(
            "INSERT INTO forgotten_sessions VALUES(?,?)",
            [(sid, f"del_{sid}") for sid in forgotten],
        )
    # This legacy inconsistency must not bypass an acknowledged forgetting record.
    assert store.get_session(forgotten[0])["status"] == "open"
    rows, coverage = metadata(store)
    assert [row["recordId"] for row in rows] == [public]
    assert coverage == [{"source": "conversations", "status": "searched"}]
