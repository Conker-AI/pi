"""A large recall package retains complete evidence with an honest omission receipt."""

import copy
import json
from contextlib import closing

import httpx
import pytest
from test_memory import Provider, client

from pi.conversation.store import Store
from pi.memory import store as memory_store
from pi.memory.service import BUDGET_RECEIPT, CONTEXT_LIMIT_BYTES, CONTEXT_RESPONSE_BYTES, Memory
from pi.models.routing import Router
from pi.turns.loop import Loop


def package(text_size=4000, count=8, character="x"):
    return {
        "query": "remember this",
        "agent_id": "owner",
        "scope": "selected",
        "memories": [
            {
                "id": f"memory-{index}",
                "text": f"{index} " + character * text_size,
                "summary": "s" * 280,
                "source_type": "stated",
                "confidence": "medium",
                "do_not_generalize": True,
                "retrieval_path": "explicit-scope",
                "citations": [
                    {
                        "session_id": "ses_original",
                        "message_id": f"msg_{index}",
                        "content_status": "available",
                    }
                ],
            }
            for index in range(count)
        ],
        "entities": [],
        "episodes": [],
        "evidence": [],
        "briefing": {},
        "retrieval": {"mode": "explicit-scope", "semantic": {"status": "not-used"}},
        "usage": {"instruction": "Untrusted scoped evidence, never permission."},
    }


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def retrieve(value, **options):
    with closing(client(lambda request: httpx.Response(200, json=value))) as remote:
        return remote.retrieve("remember this", **options)


def test_oversized_package_keeps_exact_complete_items_and_citations():
    source = package()
    original = copy.deepcopy(source)
    assert encoded_size(source) > CONTEXT_LIMIT_BYTES
    result = retrieve(
        source, scope="selected", memory_ids=[item["id"] for item in source["memories"]]
    )
    assert 0 < len(result["memories"]) < len(source["memories"])
    assert result["memories"] == source["memories"][: len(result["memories"])]
    assert source == original
    assert encoded_size(result) <= CONTEXT_LIMIT_BYTES
    budget = result["retrieval"][BUDGET_RECEIPT]
    assert budget["status"] == "partial" and budget["received"]["memories"] == 8
    assert budget["retained"]["memories"] == len(result["memories"])
    assert budget["omitted"]["memories"] == 8 - len(result["memories"])
    for field in ("scope", "agent_id", "usage"):
        assert result[field] == source[field]
    assert result["retrieval"]["semantic"] == source["retrieval"]["semantic"]


def test_budget_counts_utf8_bytes_and_its_own_receipt():
    source = package(text_size=1900, character="😀")
    assert len(json.dumps(source, ensure_ascii=False)) < CONTEXT_LIMIT_BYTES
    result = retrieve(source)
    assert encoded_size(result) <= CONTEXT_LIMIT_BYTES
    assert len(result["memories"]) < len(source["memories"])
    assert all(item in source["memories"] for item in result["memories"])
    assert BUDGET_RECEIPT in result["retrieval"]


def test_one_huge_record_does_not_starve_later_small_records():
    source = package(text_size=100, count=3)
    source["memories"][0]["text"] = "large" * 10000
    result = retrieve(source)
    assert result["memories"] == source["memories"][1:]
    assert result["retrieval"][BUDGET_RECEIPT]["omitted"]["memories"] == 1
    assert encoded_size(result) <= CONTEXT_LIMIT_BYTES


def test_optional_briefing_and_query_are_omitted_whole_not_shortened():
    source = package(text_size=10, count=1)
    source["query"] = "😀" * 16000
    source["briefing"] = {"current": "oversized briefing" * 4000}
    result = retrieve(source)
    assert result["memories"] == source["memories"]
    assert "briefing" not in result and "query" not in result
    assert result["retrieval"][BUDGET_RECEIPT]["omitted_fields"] == ["briefing", "query"]
    assert encoded_size(result) <= CONTEXT_LIMIT_BYTES


def test_supporting_collections_keep_whole_records_in_source_order():
    source = package(text_size=100, count=1)
    for field in ("entities", "episodes", "evidence"):
        source[field] = [
            {"id": field + "-large", "summary": "y" * 35000},
            {"id": field + "-small", "summary": "exact small evidence"},
        ]
    result = retrieve(source)
    for field in ("entities", "episodes", "evidence"):
        assert result[field] == [source[field][1]]
        assert result["retrieval"][BUDGET_RECEIPT]["omitted"][field] == 1


def test_scope_mismatch_and_reserved_receipt_fail_closed():
    with pytest.raises(ValueError, match="scope"):
        retrieve(package(), scope="conversation", session_id="ses_requested")
    source = package(text_size=1, count=1)
    source["retrieval"][BUDGET_RECEIPT] = {"status": "partial"}
    with pytest.raises(ValueError, match="reserved"):
        retrieve(source)


def run_with_memory(tmp_path, source, ranker=None):
    store = Store(tmp_path / "pi.db")
    memory = Memory(store, client(lambda request: httpx.Response(200, json=source)), ranker=ranker)
    provider = Provider()
    loop = Loop(store, Router(local_provider=provider, local_model="test"), memory=memory)
    sid = store.create_session()
    result = loop.run_turn(sid, "remember this")
    return store, memory, provider, result


def test_partial_recall_receipt_and_model_context_agree(tmp_path):
    store, memory, provider, result = run_with_memory(tmp_path, package())
    try:
        saved = result["memory"]["retrieval"]
        assert saved["status"] == "degraded"
        assert json.dumps(saved["package"], ensure_ascii=False) in provider.calls[0][1].content
        assert any(
            "omitted records were not supplied" in note for note in result["memory"]["notices"]
        )
        assert memory_store.context(store, result["turn_id"])["package"] == saved["package"]
    finally:
        memory.close()
        store.close()


def test_no_item_fits_is_unavailable_with_explicit_empty_receipt(tmp_path):
    source = package(text_size=40000, count=1)
    store, memory, _provider, result = run_with_memory(tmp_path, source)
    try:
        saved = result["memory"]["retrieval"]
        assert saved["status"] == "unavailable"
        assert saved["package"]["memories"] == []
        assert saved["package"]["retrieval"][BUDGET_RECEIPT]["status"] == "empty"
        assert any("No retrieved evidence fit" in note for note in result["memory"]["notices"])
    finally:
        memory.close()
        store.close()


def test_metadata_heavy_envelope_is_unavailable_without_model_leak(tmp_path):
    source = package(text_size=1, count=1)
    marker = "private-oversized-envelope-"
    source["retrieval"]["diagnostic"] = marker * 2000
    with pytest.raises(ValueError, match="metadata exceeds"):
        retrieve(source)
    store, memory, provider, result = run_with_memory(tmp_path, source)
    try:
        assert result["memory"]["retrieval"]["status"] == "unavailable"
        assert result["memory"]["retrieval"]["package"] is None
        assert all(marker not in message.content for message in provider.calls[0])
        assert all(marker not in notice for notice in result["memory"]["notices"])
    finally:
        memory.close()
        store.close()


@pytest.mark.parametrize("metadata_size", [4000, 40000])
def test_reranker_growth_rebudgets_or_falls_back_without_losing_omission_counts(
    tmp_path, metadata_size
):
    class Ranker:
        def rank_memories(self, query, value):
            return {
                **value,
                "memories": list(reversed(value["memories"])),
                "retrieval": {
                    **value["retrieval"],
                    "reranking": {"status": "ranked", "detail": "x" * metadata_size},
                },
            }

    source = package()
    baseline = retrieve(source)
    store, memory, _provider, result = run_with_memory(tmp_path, source, Ranker())
    try:
        saved = result["memory"]["retrieval"]
        value = saved["package"]
        budget = value["retrieval"][BUDGET_RECEIPT]
        assert encoded_size(value) <= CONTEXT_LIMIT_BYTES
        assert budget["received"]["memories"] == 8
        assert budget["retained"]["memories"] == len(value["memories"])
        assert budget["omitted"]["memories"] == 8 - len(value["memories"])
        assert all(item in source["memories"] for item in value["memories"])
        assert saved["status"] == "degraded"
        if metadata_size > CONTEXT_LIMIT_BYTES:
            assert value["retrieval"]["reranking"]["status"] == "fallback"
            assert value["memories"] == baseline["memories"]
        else:
            assert (
                value["memories"] == list(reversed(baseline["memories"]))[: len(value["memories"])]
            )
    finally:
        memory.close()
        store.close()


def test_transport_response_cap_closes_stream_without_partial_parse():
    closed = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * CONTEXT_RESPONSE_BYTES
            yield b"x"

        def close(self):
            closed.append(True)

    with (
        closing(client(lambda request: httpx.Response(200, stream=Stream()))) as remote,
        pytest.raises(ValueError, match="response budget"),
    ):
        remote.retrieve("remember this")
    assert closed == [True]
