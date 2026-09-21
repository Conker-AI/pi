"""Actual supplied citation provenance; no inferred citations from retrieval."""

import sqlite3

import pytest
from pydantic import ValidationError

from pi import artifacts as a
from pi import citations as c
from pi.store import Store

EVIDENCE = [
    {
        "id": "reference-1",
        "label": "Supplied source",
        "href": "https://example.org/source",
        "excerpt": "Evidence ``` stays inert",
    }
]


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "citations.db")
    yield value
    value.close()


def completed(store, evidence=EVIDENCE):
    session = store.create_session()
    turn = store.start_turn(session)
    message = store.complete_turn(turn, "source", citations=evidence)
    return session, message


def privacy(db, session_id):
    return {"memoryDisabled": False, "harnessDisabled": True}


@pytest.mark.parametrize(
    "value",
    [
        [{"id": "a", "label": "A", "href": "javascript:alert(1)"}],
        [{"id": "a", "label": "A", "href": "https://name:secret@example.org"}],
        [{"id": "a", "label": "A", "href": "file:///secret"}],
        [{"id": "a", "label": "A", "href": "https://example.org/\nsecret"}],
        [{"id": "a", "label": "A", "execute": True}],
        [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}],
        [{"id": "a", "label": "A", "excerpt": "x" * 8001}],
        [{"id": str(i), "label": "A", "excerpt": "x" * 8000} for i in range(20)],
        [{"id": str(i), "label": "A"} for i in range(101)],
    ],
)
def test_strict_citation_contract(value):
    with pytest.raises((ValueError, ValidationError)):
        c.normalize(value)


def test_evidence_persistence_and_database_immutability(store):
    _, message = completed(store)
    with store._connect() as db:
        assert c.read(db, message["id"]) == EVIDENCE
        for sql in (
            "UPDATE message_citations SET body='[]'",
            "DELETE FROM message_citations",
            "INSERT OR REPLACE INTO message_citations SELECT * FROM message_citations",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
    path = store.path
    store.close()
    reopened = Store(path)
    try:
        with reopened._connect() as db:
            assert c.read(db, message["id"]) == EVIDENCE
    finally:
        reopened.close()


def test_nonfinal_messages_cannot_acquire_citations(store):
    session = store.create_session()
    message = store.append_message(session, "assistant", "legacy")
    with store._connect() as db, pytest.raises(sqlite3.IntegrityError):
        c.save(db, message["id"], EVIDENCE)


def test_artifact_carries_edits_drops_and_restores_evidence(store):
    session, message = completed(store)
    created = a.create(
        store, a.FromMessage(title="Evidence", sessionId=session, messageId=message["id"]), privacy
    )
    identity = created["id"]
    assert created["versions"][0]["citations"] == EVIDENCE
    edited = a.mutate(
        store,
        identity,
        a.Append(expected_revision=1, content={"kind": "markdown", "text": "edited"}),
        privacy,
    )
    assert edited["versions"][-1]["citations"] == EVIDENCE
    removed = a.mutate(
        store,
        identity,
        a.Append(
            expected_revision=2,
            content={"kind": "markdown", "text": "no evidence"},
            preserveCitations=False,
        ),
        privacy,
    )
    assert "citations" not in removed["versions"][-1]
    restored = a.mutate(store, identity, a.Restore(expected_revision=3, version=1), privacy)
    assert restored["versions"][-1]["citations"] == EVIDENCE
    code = a.mutate(
        store,
        identity,
        a.Append(expected_revision=4, content={"kind": "code", "language": "py", "text": "pass"}),
        privacy,
    )
    assert "citations" not in code["versions"][-1]
    exported = a.export(store, identity, version=4, resolve=privacy)
    assert "not independently verified" in exported["text"]
    assert "Evidence \\u0060\\u0060\\u0060 stays inert" in exported["text"]
    assert a.get(store, identity)["versions"] == []
    with pytest.raises(a.ArtifactError):
        a.export(store, identity)


def test_source_evidence_change_blocks_stale_artifact_and_redaction(store):
    session, message = completed(store)
    identity = a.create(
        store, a.FromMessage(title="Evidence", sessionId=session, messageId=message["id"]), privacy
    )["id"]
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        c.redact(db, [session])
        db.commit()
        assert c.read(db, message["id"]) == []
        assert db.execute("SELECT body FROM message_citations").fetchone()[0] is None
    assert a.get(store, identity, privacy)["availability"] == "source-changed"
    with pytest.raises(a.ArtifactError):
        a.export(store, identity, resolve=privacy)


def test_store_citation_validation_rolls_back_reply_and_completion(store):
    session = store.create_session()
    turn = store.start_turn(session)
    with pytest.raises(ValueError):
        store.complete_turn(
            turn,
            "must not persist",
            citations=[{"id": "bad", "label": "Bad", "href": "javascript:bad()"}],
        )
    assert store.messages(session) == []
    assert store.get_turn(turn)["status"] == "running"
    saved = store.complete_turn(turn, "valid reply", citations=EVIDENCE)
    assert store.get_message(saved["id"])["citations"] == EVIDENCE
    assert store.messages(session)[0]["citations"] == EVIDENCE


def test_loop_persists_only_explicit_provider_evidence(store):
    from pi.loop import Loop, TurnFailed
    from pi.providers import Completion
    from pi.routing import Router

    class Provider:
        name = "test"
        evidence = EVIDENCE

        def complete(self, messages, *, model):
            return Completion(
                text="supplied response", model=model, provider=self.name, citations=self.evidence
            )

    provider = Provider()
    loop = Loop(store, Router(local_provider=provider, local_model="test"))
    session = store.create_session()
    result = loop.run_turn(session, "question")
    assert result["message"]["citations"] == EVIDENCE
    assert store.get_message(result["message"]["id"])["citations"] == EVIDENCE
    provider.evidence = [{"id": "invalid", "label": "bad", "href": "file:///secret"}]
    with pytest.raises(TurnFailed):
        loop.run_turn(session, "bad evidence")
    assert store.turns(session)[-1]["status"] == "failed"
    assert len([m for m in store.messages(session) if m["role"] == "assistant"]) == 1


def test_actual_forgetting_erases_citation_metadata_and_artifact_copies(store):
    from pi import forgetting

    evidence = [
        {
            "id": "erase-id-8573",
            "label": "erase-label-8573",
            "href": "https://example.org/erase-url-8573",
            "excerpt": "erase-excerpt-8573",
        }
    ]
    session, message = completed(store, evidence)
    artifact = a.create(
        store, a.FromMessage(title="Evidence", sessionId=session, messageId=message["id"]), privacy
    )
    a.mutate(
        store,
        artifact["id"],
        a.Append(expected_revision=1, content={"kind": "markdown", "text": "edited"}),
        privacy,
    )
    path = store.path
    store.close()
    forgetting.forget(path, session, forgetting.preview(path, session)["confirmation"])
    reopened = Store(path)
    try:
        assert "citations" not in reopened.get_message(message["id"])
        assert a.get(reopened, artifact["id"], privacy)["versions"] == []
        with reopened._connect() as db:
            assert c.read(db, message["id"]) == []
    finally:
        reopened.close()
    for file in path.parent.glob("citations.db*"):
        for secret in evidence[0].values():
            assert secret.encode() not in file.read_bytes()


def test_openrouter_maps_supplied_annotations_without_enabling_search(monkeypatch):
    import httpx
    from test_audit_models import catalogue

    from pi.providers import Message, ProviderUnavailable

    provider = catalogue(monkeypatch, {"prompt": "0", "completion": "0", "request": "0"})
    supplied = {
        "url": "https://example.org/source",
        "title": "Supplied source",
        "content": "supplied excerpt",
        "start_index": 0,
        "end_index": 5,
    }
    requests = []

    def reply(*args, **kwargs):
        requests.append(kwargs["json"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", "https://chat"),
            json={
                "choices": [
                    {
                        "message": {
                            "content": "hello",
                            "annotations": [{"type": "url_citation", "url_citation": supplied}],
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx, "post", reply)
    completion = provider.complete([Message("user", "hello")], model="candidate")
    assert completion.citations == [
        {
            "id": "url-citation-1",
            "label": supplied["title"],
            "href": supplied["url"],
            "excerpt": supplied["content"],
        }
    ]
    assert "plugins" not in requests[0] and "tools" not in requests[0]
    supplied["url"] = "javascript:bad()"
    with pytest.raises(ProviderUnavailable, match="invalid citation"):
        provider.complete([Message("user", "hello")], model="candidate")
    assert c.from_openrouter_annotations(None) == []
    assert c.from_openrouter_annotations([{"type": "unrelated"}]) == []
