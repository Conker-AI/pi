"""Older chats remain reachable through bounded metadata pages and literal search."""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager

import pytest
from fastapi.testclient import TestClient
from test_gateway_api import RUNTIME, gateway, sign_in  # noqa: F401

from pi import api, browser_contract, forgetting, session_history, session_observation
from pi.store import Store
from pi.tasks import TaskError


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def populate(store, count=307):
    with store._connect() as db:
        db.executemany(
            "INSERT INTO sessions(id,title,status,created_at,summary) VALUES(?,?,'open',?,?)",
            [(f"ses_{i:04}", f"Title {i}", i // 3, f"Summary {i}") for i in range(count)],
        )
    return [f"ses_{i:04}" for i in reversed(range(count))]


def walk(store, **kwargs):
    rows, cursor, seen = [], None, set()
    while True:
        result = session_history.page(store, cursor=cursor, **kwargs)
        rows.extend(result["results"])
        cursor = result["next_cursor"]
        if cursor is None:
            return rows
        assert cursor not in seen and len(cursor) <= session_history.MAX_CURSOR
        seen.add(cursor)


def test_more_than_200_chats_and_tied_timestamps_have_no_gaps_or_duplicates(store):
    expected = populate(store)
    rows = walk(store, limit=17)
    assert [row["id"] for row in rows] == expected
    assert len({row["id"] for row in rows}) == 307
    assert all(
        set(row)
        == {
            "id",
            "parent_id",
            "title",
            "status",
            "created_at",
            "closed_at",
            "summary_preview",
            "content_status",
            "title_truncated",
            "summary_truncated",
        }
        for row in rows
    )
    assert all(
        row["content_status"] == "available"
        and not row["title_truncated"]
        and not row["summary_truncated"]
        for row in rows
    )
    assert len(store.list_sessions(200)) == 200


def test_insertion_before_cursor_does_not_shift_or_repeat_loaded_pages(store):
    expected = populate(store)
    first = session_history.page(store, limit=23)
    with store._connect() as db:
        db.execute("INSERT INTO sessions(id,title,created_at) VALUES('ses_new','New',999)")
        # An insertion after the cursor is visible on a later page; no full-history
        # snapshot is promised, but existing rows are neither skipped nor repeated.
        db.execute("INSERT INTO sessions(id,title,created_at) VALUES('ses_middle','Middle',10.5)")
    rows = list(first["results"])
    cursor = first["next_cursor"]
    while cursor:
        following = session_history.page(store, cursor=cursor, limit=23)
        rows.extend(following["results"])
        cursor = following["next_cursor"]
    ids = [row["id"] for row in rows]
    assert "ses_new" not in ids and ids.count("ses_middle") == 1
    assert [identity for identity in ids if identity != "ses_middle"] == expected
    assert session_history.page(store)["results"][0]["id"] == "ses_new"


def test_search_filters_all_session_metadata_before_page_limit(store):
    populate(store)
    with store._connect() as db:
        db.execute("UPDATE sessions SET title='Old unique title' WHERE id='ses_0001'")
        db.execute(
            "UPDATE sessions SET summary=? WHERE id='ses_0000'",
            ("x" * 20_000 + "Old unique summary",),
        )
    assert not any("Old unique" in row["title"] for row in store.list_sessions(200))
    result = walk(store, q="old UNIQUE", limit=1)
    assert [row["id"] for row in result] == ["ses_0001", "ses_0000"]
    assert result[-1]["summary_preview"] == "x" * 400
    assert result[-1]["summary_truncated"]
    assert session_history.page(store, q="no such retained metadata")["results"] == []


@pytest.mark.parametrize(
    "title,query",
    [
        ("Обсудим ПРИВЕТ", "привет"),
        ("Straße plan", "STRASSE"),
        ("פרטים שלום", "שלום"),
        ("İstanbul", "i\u0307STANBUL"),
        ("Keep 100%_literal\\path", "%_literal\\"),
        ("quote ' OR 1=1 -- retained", "' OR 1=1 --"),
    ],
)
def test_unicode_casefold_and_sql_metacharacters_are_literal(store, title, query):
    wanted = store.create_session(title=title)
    store.create_session(title="Unrelated 1000 literal path")
    result = session_history.page(store, q=query)
    assert [row["id"] for row in result["results"]] == [wanted]


def test_cursor_binds_normalized_query_but_not_page_size(store):
    for _ in range(4):
        store.create_session(title="Straße")
    first = session_history.page(store, q=" Straße ", limit=1)
    cursor = first["next_cursor"]
    following = session_history.page(store, q="STRASSE", cursor=cursor, limit=3)
    assert len(following["results"]) == 3 and following["next_cursor"] is None
    assert first["results"][0]["id"] not in {row["id"] for row in following["results"]}
    assert "Straße" not in cursor and "STRASSE" not in cursor
    with pytest.raises(TaskError) as error:
        session_history.page(store, q="different", cursor=cursor)
    assert error.value.status == 422 and error.value.detail["code"] == "invalid_history_cursor"


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "wrong",
        "h1." + "0" * 64 + ".ses_missing",
        "x" * 197,
        "../private",
        "h1." + "g" * 64 + ".ses_x",
        "h1." + "0" * 64 + ".日本語",
    ],
)
def test_malformed_oversized_and_mismatched_cursor_rejected_before_read(store, monkeypatch, cursor):
    def forbidden():
        pytest.fail("Invalid cursor must be rejected before opening the database")

    monkeypatch.setattr(store, "_connect", forbidden)
    with pytest.raises(TaskError) as error:
        session_history.page(store, cursor=cursor)
    assert error.value.status == 422 and error.value.detail["code"] == "invalid_history_cursor"


def test_missing_cursor_anchor_has_fixed_recoverable_error(store):
    cursor = "h1." + hashlib.sha256(b"").hexdigest() + ".ses_missing"
    with pytest.raises(TaskError) as error:
        session_history.page(store, cursor=cursor)
    assert error.value.detail == {
        "code": "invalid_history_cursor",
        "message": "History cursor unavailable; restart this search.",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 51},
        {"limit": True},
        {"q": "🙂" * 201},
        {"q": 7},
        {"q": "a\x00b"},
        {"q": "\ud800"},
    ],
)
def test_invalid_query_and_limit_rejected_before_read(store, monkeypatch, kwargs):
    def forbidden():
        pytest.fail("Invalid request must be rejected before opening the database")

    monkeypatch.setattr(store, "_connect", forbidden)
    with pytest.raises(TaskError) as error:
        session_history.page(store, **kwargs)
    assert error.value.status == 422 and error.value.detail["code"] == "invalid_history_request"


def test_previews_are_bounded_by_codepoints_and_do_not_rewrite_legacy_content(store):
    title, summary = "🙂" * 301, "界\x00" * 201
    sid = store.create_session(title=title, summary=summary)
    row = session_history.page(store)["results"][0]
    assert row["title"] == title[:300] and row["summary_preview"] == summary[:400]
    assert row["title_truncated"] and row["summary_truncated"]
    assert store.get_session(sid)["title"] == title and store.get_session(sid)["summary"] == summary
    other = store.create_session(title="short", summary=None)
    rows = {row["id"]: row for row in session_history.page(store)["results"]}
    assert rows[other]["summary_preview"] is None
    assert not rows[other]["title_truncated"] and not rows[other]["summary_truncated"]


def test_maximum_page_payload_stays_bounded_with_escaped_legacy_text(store):
    with store._connect() as db:
        db.executemany(
            "INSERT INTO sessions(id,title,summary,created_at) VALUES(?,?,?,0)",
            [(f"ses_{i:04}", "\x01" * 1000, "\x02" * 1000) for i in range(51)],
        )
    result = session_history.page(store)
    assert len(result["results"]) == 50 and result["next_cursor"]
    assert len(json.dumps(result, ensure_ascii=False).encode()) < session_history.MAX_PAGE_BYTES
    assert all(
        len(row["title"]) == 300 and len(row["summary_preview"]) == 400 for row in result["results"]
    )


def test_private_chat_owner_access_is_preserved_without_widening_global_search(store):
    from pi import universal_search

    sid = store.create_session(
        title="Private Needle",
        summary="Private summary",
        privacy={"memoryDisabled": True, "harnessDisabled": True},
    )
    store.append_message(sid, "user", "Private transcript is not a directory field")
    result = session_history.page(store, q="needle")
    assert [row["id"] for row in result["results"]] == [sid]
    assert result["results"][0]["summary_preview"] == "Private summary"
    assert "transcript" not in json.dumps(result)
    matches, _ = universal_search.lexical(
        store, None, None, "needle", {"sources": ["conversations"]}, "metadata"
    )
    assert matches == []


def test_forgotten_tombstone_masks_inconsistent_legacy_content_and_search(store):
    sid = store.create_session(title="Secret needle", summary="Secret summary")
    with store._connect() as db:
        db.execute(
            "INSERT INTO forgetting_receipts VALUES('del_history',?,0,'confirmed',0,0)", (sid,)
        )
        db.execute("INSERT INTO forgotten_sessions VALUES(?,'del_history')", (sid,))
    row = session_history.page(store)["results"][0]
    assert (
        row["id"] == sid and row["status"] == "forgotten" and row["content_status"] == "forgotten"
    )
    assert row["title"] == "" and row["summary_preview"] is None
    assert not row["title_truncated"] and not row["summary_truncated"]
    assert "Secret" not in json.dumps(row)
    assert session_history.page(store, q="needle")["results"] == []


def test_real_forgetting_preserves_cursor_boundary_and_clears_search(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as original:
        for _ in range(3):
            original.create_session(title="needle")
        first = session_history.page(original, q="needle", limit=1)
        sid = first["results"][0]["id"]
        cursor = first["next_cursor"]
    forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])
    with closing(Store(path)) as reopened:
        following = session_history.page(reopened, q="needle", cursor=cursor)
        assert len(following["results"]) == 2 and sid not in {
            row["id"] for row in following["results"]
        }
        assert all(row["content_status"] == "available" for row in following["results"])


def test_history_index_migrates_repeatedly_without_changing_data_or_observation(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as original:
        populate(original)
        original.append_message("ses_0000", "user", "Retained original")
        expected = session_history.page(original, limit=50)
        observation = session_observation.read(original, "ses_0000")
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("DROP INDEX sessions_history_order")
        # This is the exact preceding release: its original creation index remains.
        # Ascending IDs within tied times differ from the new directory's ID DESC.
        legacy = [
            dict(row)
            for row in db.execute("SELECT * FROM sessions ORDER BY created_at DESC LIMIT 200")
        ]
    for _ in range(2):
        with closing(Store(path)) as reopened:
            assert session_history.page(reopened, limit=50) == expected
            assert reopened.messages("ses_0000")[0]["content"] == "Retained original"
            assert session_observation.read(reopened, "ses_0000") == observation
            assert reopened.list_sessions(200) == legacy
            with reopened._connect() as db:
                db.execute("ANALYZE")
            assert reopened.list_sessions(200) == legacy


def test_actual_directory_query_is_indexed_bounded_and_read_only(store, monkeypatch):
    populate(store)
    queries, plans = [], []
    connect = store._connect

    @contextmanager
    def traced():
        with connect() as db:
            db.set_trace_callback(queries.append)
            yield db
            # The actual read connection owns the registered Unicode/preview UDFs.
            sql = next(
                statement for statement in reversed(queries) if statement.startswith("SELECT s.id")
            )
            plans.append(" ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql)))

    monkeypatch.setattr(store, "_connect", traced)
    with connect() as monitor:
        version = monitor.execute("PRAGMA data_version").fetchone()[0]
        result = session_history.page(store, limit=10)
        following = session_history.page(store, limit=10, cursor=result["next_cursor"])
        assert len(following["results"]) == 10
        assert monitor.execute("PRAGMA data_version").fetchone()[0] == version
        selects = [sql for sql in queries if sql.startswith("SELECT s.id")]
        assert len(selects) == 2 and all(sql.endswith("LIMIT 11") for sql in selects)
        for plan in plans:
            assert "sessions_history_order" in plan and "TEMP B-TREE" not in plan
        assert all("FROM messages" not in sql and "memory" not in sql for sql in queries)


def test_api_runtime_auth_static_route_and_strict_query_contract(store, monkeypatch):
    populate(store)
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "recovery-key", raising=False)
    monkeypatch.setattr(
        api.app.state, "gateway_key_hash", hashlib.sha256(b"runtime-key").hexdigest(), raising=False
    )
    client = TestClient(api.app)
    path = "/sessions/history"
    headers = {"X-Pi-Gateway-Key": "runtime-key"}
    assert browser_contract.runtime_allowed("GET", path)
    assert not browser_contract.runtime_allowed("POST", path)
    assert client.get(path).status_code == 401
    response = client.get(path, headers=headers)
    assert response.status_code == 200 and response.json() == session_history.page(store)
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get(path, headers={"X-Pi-Key": "recovery-key"}).json() == response.json()
    for query in (
        "limit=0",
        "limit=51",
        "limit=-1",
        "limit=1.5",
        "limit=true",
        "limit=100000",
        "limit=1&limit=2",
        "q=a&q=b",
        "q=" + "x" * 201,
        "q=%00",
        "private=true",
    ):
        result = client.get(path + "?" + query, headers=headers)
        assert result.status_code == 422
        assert result.json()["detail"]["code"] == "invalid_history_request"
    for query in ("cursor=", "cursor=" + "x" * 197):
        result = client.get(path + "?" + query, headers=headers)
        assert (
            result.status_code == 422
            and result.json()["detail"]["code"] == "invalid_history_cursor"
        )
    assert client.get(path + "?q=%20%20", headers=headers).json() == response.json()


def test_gateway_directory_read_uses_only_scoped_runtime_credential(gateway):
    client, _, seen = gateway
    path = "/api/pi/sessions/history"
    assert client.get(path).status_code == 401 and not seen
    headers = sign_in(client)
    response = client.get(
        path + "?limit=7&q=old",
        headers={"X-Pi-Key": "browser-secret", "X-Pi-Gateway-Key": "browser-secret"},
    )
    assert response.status_code == 200 and len(seen) == 1
    assert seen[0].url.path == "/sessions/history" and seen[0].url.params["limit"] == "7"
    assert seen[0].headers["X-Pi-Gateway-Key"] == RUNTIME and "X-Pi-Key" not in seen[0].headers
    assert "Set-Cookie" not in response.headers
    assert client.post(path, json={}, headers=headers).status_code == 403 and len(seen) == 1
