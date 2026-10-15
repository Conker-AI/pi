"""Literal matches use original-text offsets when building bounded previews."""

from contextlib import closing

import pytest
from test_universal_search import client_for

from pi import conversation_search, universal_search
from pi.store import Store


@pytest.mark.parametrize(
    "prefix",
    ["ß" * 2000, "ﬃ" * 2000, "İ" * 2000, "🙂ß" * 1000],
    ids=["sharp-s", "ligature", "dotted-i", "astral"],
)
def test_expanding_prefix_preserves_direct_and_global_exact_match(tmp_path, prefix):
    with closing(Store(tmp_path / "search.db")) as store:
        sid = store.create_session(title="Planning")
        message = store.append_message(sid, "user", prefix + "Needle" + "z" * 2000)
        direct = conversation_search.search(store, "needle")["results"]
        assert len(direct) == 1 and direct[0]["id"] == message["id"]
        assert "Needle" in direct[0]["excerpt"] and len(direct[0]["excerpt"]) <= 500
        page = client_for(store).get("/search", params={"q": "needle", "stage": "text"}).json()
        assert [row["recordId"] for row in page["results"]] == [message["id"]]
        assert "Needle" in page["results"][0]["excerpt"]
        assert len(page["results"][0]["excerpt"]) <= 400
        assert page["results"][0]["href"] == f"/chat?session={sid}&message={message['id']}"


@pytest.mark.parametrize(
    "prefix", ["ß" * 2000, "ﬃ" * 2000, "İ" * 2000], ids=["sharp-s", "ligature", "dotted-i"]
)
def test_metadata_summary_preview_uses_original_offsets(tmp_path, prefix):
    with closing(Store(tmp_path / "metadata.db")) as store:
        sid = store.create_session(title="Planning", summary=prefix + "Needle" + "z" * 2000)
        rows, coverage = universal_search.conversation_metadata(store, "needle")
        assert [row["recordId"] for row in rows] == [sid]
        assert "Needle" in rows[0]["excerpt"] and len(rows[0]["excerpt"]) <= 400
        assert coverage is False


def test_verified_full_text_match_survives_query_larger_than_preview(tmp_path):
    # A 200-character query can expand to 600 characters after casefolding.
    query = "ﬃ" * 200
    with closing(Store(tmp_path / "long.db")) as store:
        sid = store.create_session(title="Long match")
        message = store.append_message(sid, "assistant", "x" * 1000 + "ffi" * 200 + "y" * 1000)
        direct = conversation_search.search(store, query)["results"]
        assert [row["id"] for row in direct] == [message["id"]]
        assert len(direct[0]["excerpt"]) <= 500
        assert query.casefold() not in direct[0]["excerpt"].casefold()
        rows, coverage = universal_search.lexical(
            store, None, None, query, {"sources": ["conversations"]}, "text"
        )
        assert [row["recordId"] for row in rows] == [message["id"]]
        assert len(rows[0]["excerpt"]) <= 400 and rows[0]["matchType"] == "text"
        assert coverage == [{"source": "conversations", "status": "searched"}]


@pytest.mark.parametrize(
    "text,query,expected",
    [
        ("x" * 1000 + "ß" + "y" * 1000, "s", "x" * 80 + "ß"),
        ("x" * 1000 + "ﬃ" + "y" * 1000, "fi", "x" * 80 + "ﬃ"),
        ("x" * 1000 + "İ" + "y" * 1000, "\u0307", "x" * 80 + "İ"),
        ("Needle" + "x" * 1000, "needle", "Needle"),
        ("x" * 1000 + "Needle", "needle", "x" * 80 + "Needle"),
        ("preview only", "title match", "preview only"),
    ],
    ids=["inside-sharp-s", "inside-ligature", "inside-dotted-i", "start", "end", "title-only"],
)
def test_preview_starts_near_original_match(text, query, expected):
    excerpt = universal_search.excerpt(text, query)
    assert excerpt.startswith(expected) and len(excerpt) <= 400


def test_unverified_source_records_still_require_literal_match():
    assert (
        universal_search.record("projects", "p1", "Other", "no match", "/projects/p1", "needle")
        is None
    )


@pytest.mark.parametrize(
    "text,query,matched",
    [
        ("prefix a\0WRONG tail", "a\0DIFFERENT", False),
        ("prefix a\0Needle tail", "needle", True),
        ("prefix a\0Needle tail", "a\0Needle", True),
        ("prefix a WRONG tail", "a\0", False),
    ],
    ids=["nul-query-mismatch", "match-after-nul", "match-across-nul", "nul-not-in-text"],
)
def test_sql_source_proves_literal_match_even_with_nul(tmp_path, text, query, matched):
    with closing(Store(tmp_path / "nul.db")) as store:
        sid = store.create_session(title="Literal")
        message = store.append_message(sid, "user", text)
        expected = [message["id"]] if matched else []
        direct = conversation_search.search(store, query)
        assert [row["id"] for row in direct["results"]] == expected
        response = client_for(store).get("/search", params={"q": query, "stage": "text"})
        assert response.status_code == 200
        assert [row["recordId"] for row in response.json()["results"]] == expected
