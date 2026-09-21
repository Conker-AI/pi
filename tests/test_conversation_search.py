from contextlib import closing

from pi import conversation_search as search
from pi import session_settings
from pi.store import Store


def test_literal_search_paginates_exact_message_links(tmp_path):
    with closing(Store(tmp_path / "search.db")) as store:
        sid = store.create_session(title="Planning")
        ids = [store.append_message(sid, "user", "Find 100%_literal")["id"] for _ in range(3)]
        store.append_message(sid, "user", "Find 1000 similar")
        first = search.search(store, "%_", limit=2)
        second = search.search(store, "%_", limit=2, cursor=first["next_cursor"])
        results = first["results"] + second["results"]
        assert {item["id"] for item in results} == set(ids)
        assert all(item["session_id"] == sid and item["title"] == "Planning" for item in results)
        assert second["next_cursor"] is None


def test_private_history_excluded_even_after_privacy_disabled(tmp_path):
    with closing(Store(tmp_path / "search.db")) as store:
        sid = store.create_session()
        settings = session_settings.Settings(
            agentId="companion",
            privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=False),
        )
        session_settings.save(
            store, sid, session_settings.Update(expected_revision=0, settings=settings)
        )
        store.append_message(sid, "user", "secret needle")
        assert not search.search(store, "needle")["results"]
        settings.privacy.memoryDisabled = False
        session_settings.save(
            store, sid, session_settings.Update(expected_revision=1, settings=settings)
        )
        assert not search.search(store, "needle")["results"]


def test_unicode_and_nontext_or_tool_data(tmp_path):
    with closing(Store(tmp_path / "search.db")) as store:
        sid = store.create_session()
        message = store.append_message(sid, "assistant", "שלום привет")
        store.append_message(sid, "tool", "שלום")
        store.append_message(sid, "assistant", {"private_payload": "שלום"})
        assert [item["id"] for item in search.search(store, "שלום")["results"]] == [message["id"]]


def test_excerpt_centers_match_and_forgetting_removes_result(tmp_path):
    from pi import forgetting

    path = tmp_path / "search.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        store.append_message(sid, "user", "x" * 2000 + "Needle" + "y" * 2000)
        result = search.search(store, "needle")["results"][0]
        assert "Needle" in result["excerpt"] and len(result["excerpt"]) <= 500
    forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])
    with closing(Store(path)) as store:
        assert search.search(store, "needle")["results"] == []
