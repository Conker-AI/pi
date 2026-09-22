import json
from pi import model_roles
from pi.memory_ranking import ConfiguredMemoryRanker
from pi.store import Store
from test_model_roles import config, Adapter


def setup(tmp_path, response, enabled=True):
    store = Store(str(tmp_path / "pi.db"))
    c = config()
    c["roleSettings"]["roles"]["memory-ranking"].update(enabled=enabled, modelId="b", eligibleModelIds=["b"])
    model_roles.save(store, model_roles.Update(expected_revision=0, configuration=c))
    adapter = Adapter(response)
    return ConfiguredMemoryRanker(store, {"two": adapter}), adapter


def package():
    return {"memories": [{"id": "first", "text": "alpha", "source": "s1"},
                         {"id": "second", "text": "beta", "source": "s2"}], "retrieval": {"mode": "text"}}


def test_ranking_uses_independent_provider_and_preserves_records(tmp_path):
    ranker, adapter = setup(tmp_path, json.dumps({"order": ["m1", "m0"]}))
    original = package()
    result = ranker.rank_memories("beta", original)
    assert result["memories"] == list(reversed(original["memories"]))
    assert adapter.calls == [("actual-b", 1.0)]
    assert result["retrieval"]["reranking"]["status"] == "ranked"
    assert original == package()


def test_disabled_never_calls_provider(tmp_path):
    ranker, adapter = setup(tmp_path, "invalid", enabled=False)
    assert ranker.rank_memories("query", package()) == package()
    assert adapter.calls == []


def test_invalid_duplicate_order_keeps_every_source(tmp_path):
    ranker, _ = setup(tmp_path, '{"order":["m0","m0"]}')
    result = ranker.rank_memories("query", package())
    assert result["memories"] == package()["memories"]
    assert result["retrieval"]["reranking"]["status"] == "fallback"


def test_legacy_configuration_migrates_disabled():
    c = config()
    c["roleSettings"]["roles"].pop("memory-ranking")
    migrated = model_roles.Configuration.model_validate(c)
    assert not migrated.roleSettings.roles["memory-ranking"].enabled
