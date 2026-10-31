"""Installed-route readiness and activation must preserve a working owner choice."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import httpx
import pytest

from pi.conversation.store import Store
from pi.models import roles as model_roles
from pi.models.providers import Completion, OllamaProvider, ProviderUnavailable
from pi.setup import model_probes as setup_model_probes
from pi.setup import models as setup_models
from pi.setup import status_steps


class Adapter:
    name = "ollama"

    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def health(self):
        return {"status": "ok"}

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append((messages, model, timeout))
        if self.callback:
            return self.callback()
        return Completion("ready", model, self.name)


class Router:
    def __init__(self, adapter=None):
        self.local = adapter or Adapter()
        self.local_model = "working:3b"

    def adapters(self):
        return {"ollama": self.local}


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as result:
        yield result


def seed(store):
    roles = {
        role: dict(
            enabled=False,
            eligibleModelIds=[],
            modelId=None,
            timeoutMs=1000,
            failure="stop",
            fallbackModelId=None,
        )
        for role in model_roles.ROLES
    }
    roles["answer"].update(enabled=True, eligibleModelIds=["working"], modelId="working")
    roles["routing"].update(enabled=True, eligibleModelIds=["laya"], modelId="laya")
    roles["proposals"]["timeoutMs"] = 7777
    configuration = dict(
        providers=[
            dict(id="ollama", name="Local", enabled=True),
            dict(id="decisions", name="Laya", enabled=True),
        ],
        models=[
            dict(
                id="working", name="Working", providerId="ollama", route="working:3b", enabled=True
            ),
            dict(id="spare", name="Spare", providerId="ollama", route="spare:4b", enabled=True),
            dict(
                id="laya", name="Laya", providerId="decisions", route="model-routing", enabled=True
            ),
        ],
        defaultModelId="working",
        roleSettings=dict(answerMode="manual", roles=roles),
    )
    return model_roles.save(
        store, model_roles.Update(expected_revision=0, configuration=configuration)
    )


def activation(request="activate-spare", revision=1, candidate="spare"):
    return setup_models.Activation(
        requestId=request, candidateId=candidate, expectedRevision=revision
    )


def inventory(monkeypatch, names):
    calls = []

    def tags(url, **kwargs):
        calls.append(url)
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"] == 5.0
        return httpx.Response(
            200,
            json={"models": [{"name": name} for name in names]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", tags)
    return calls


def test_catalogue_readiness_uses_one_inventory_and_each_exact_route(store, monkeypatch):
    seed(store)
    calls = inventory(monkeypatch, ["working:3b"])
    router = Router(OllamaProvider("http://127.0.0.1:11434", model="working:3b"))

    found = setup_models.options(store, router)

    assert {item.id: item.status for item in found.candidates} == {
        "working": "ready",
        "spare": "unavailable",
    }
    assert len(calls) == 1
    before = model_roles.load(store)
    with pytest.raises(RuntimeError, match="not ready"):
        setup_models.activate(store, router, activation())
    assert model_roles.load(store) == before
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM setup_model_probe_requests").fetchone()[0] == 0


def test_missing_adapter_default_does_not_hide_an_installed_alternative(store, monkeypatch):
    seed(store)
    inventory(monkeypatch, ["spare:4b"])
    router = Router(OllamaProvider("http://127.0.0.1:11434", model="working:3b"))
    found = setup_models.options(store, router)
    assert {item.id: item.status for item in found.candidates} == {
        "working": "unavailable",
        "spare": "ready",
    }


def test_model_health_retains_exact_tag_and_latest_alias_semantics(monkeypatch):
    calls = inventory(monkeypatch, ["tiny:latest", "working:3b"])
    result = OllamaProvider("http://127.0.0.1:11434").health_models(
        ["tiny", "tiny:latest", "working", "working:3b", "working:8b"]
    )
    assert {route for route, health in result.items() if health["status"] == "ok"} == {
        "tiny",
        "tiny:latest",
        "working:3b",
    }
    assert len(calls) == 1


def test_setup_readiness_drops_after_selected_route_disappears(store, monkeypatch):
    seed(store)
    setup_models.activate(store, Router(), activation())
    calls = inventory(monkeypatch, ["working:3b"])
    router = Router(OllamaProvider("http://127.0.0.1:11434", model="working:3b"))

    status = status_steps._model(store, router)

    assert status.state == "degraded"
    assert status.blockingReasonCode == "model_provider_unavailable"
    assert len(calls) == 1
    assert setup_model_probes.current(store, 2, "spare") is not None


@pytest.mark.parametrize(
    "result,code",
    [
        (ProviderUnavailable("sensitive provider detail"), "provider_unavailable"),
        (RuntimeError("sensitive provider detail"), "provider_error"),
        (Completion("", "spare:4b", "ollama"), "invalid_response"),
        (Completion("ready", "", "ollama"), "invalid_response"),
        (Completion("x" * 16001, "spare:4b", "ollama"), "invalid_response"),
        (Completion("ready", "x" * 301, "ollama"), "invalid_response"),
        (Completion("\ud800", "spare:4b", "ollama"), "invalid_response"),
        (Completion("ready", "\ud800", "ollama"), "invalid_response"),
        (Completion("ready", "spare\x1b[2J", "ollama"), "invalid_response"),
        (None, "invalid_response"),
    ],
)
def test_failed_activation_preserves_complete_previous_configuration(store, result, code):
    before = seed(store)

    def response():
        assert model_roles.load(store) == before
        if isinstance(result, Exception):
            raise result
        return result

    router = Router(Adapter(response))
    with pytest.raises(setup_model_probes.ProbeError) as failure:
        setup_models.activate(store, router, activation())
    assert failure.value.code == code
    assert "sensitive" not in str(failure.value)
    assert model_roles.load(store) == before
    assert setup_model_probes.current(store, 2, "spare") is None
    with pytest.raises(setup_model_probes.ProbeError) as retry:
        setup_models.activate(store, router, activation())
    assert retry.value.code == "probe_not_replayable"
    assert len(router.local.calls) == 1


def test_failed_fresh_setup_leaves_no_configuration(store):
    router = Router(Adapter(lambda: Completion("", "working:3b", "ollama")))
    with pytest.raises(setup_model_probes.ProbeError):
        setup_models.activate(store, router, activation(revision=0, candidate="local-answer"))
    assert model_roles.load(store) == {"revision": 0, "configuration": None}


def test_activation_commits_settings_and_receipt_together_and_replay_does_not_restore(store):
    before = seed(store)
    router = Router()
    result = setup_models.activate(store, router, activation())
    after = model_roles.load(store)
    assert result.revision == after["revision"] == 2
    assert after["configuration"]["defaultModelId"] == "spare"
    for role in model_roles.ROLES:
        if role != "answer":
            assert (
                after["configuration"]["roleSettings"]["roles"][role]
                == before["configuration"]["roleSettings"]["roles"][role]
            )
    assert setup_model_probes.current(store, 2, "spare") == result.probe
    later = model_roles.save(
        store, model_roles.Update(expected_revision=2, configuration=before["configuration"])
    )

    class NoCalls:
        def adapters(self):
            pytest.fail("Receipt replay must not call adapters or probe again")

    assert setup_models.activate(store, NoCalls(), activation()) == result
    assert model_roles.load(store) == later
    assert len(router.local.calls) == 1


def test_duplicate_activation_during_probe_does_not_invoke_again(store):
    before = seed(store)

    def during_probe():
        assert model_roles.load(store) == before
        with pytest.raises(setup_model_probes.ProbeError) as duplicate:
            setup_models.activate(store, router, activation())
        assert duplicate.value.code == "probe_not_replayable"
        return Completion("ready", "spare:4b", "ollama")

    router = Router(Adapter(during_probe))
    first = setup_models.activate(store, router, activation())
    assert setup_models.activate(store, router, activation()) == first
    assert len(router.local.calls) == 1


def test_owner_update_during_probe_wins_and_no_receipt_is_published(store):
    before = seed(store)

    def update_during_probe():
        updated = before["configuration"]
        updated["roleSettings"]["roles"]["proposals"]["timeoutMs"] = 8765
        model_roles.save(store, model_roles.Update(expected_revision=1, configuration=updated))
        return Completion("ready", "spare:4b", "ollama")

    router = Router(Adapter(update_during_probe))
    with pytest.raises(setup_model_probes.ProbeError) as failure:
        setup_models.activate(store, router, activation())
    assert failure.value.code == "revision_conflict"
    assert failure.value.status == 409
    after = model_roles.load(store)
    assert after["revision"] == 2
    assert after["configuration"] == before["configuration"]
    assert setup_model_probes.current(store, 2, "spare") is None
    assert len(router.local.calls) == 1


def test_revision_change_before_claim_prevents_inference(store, monkeypatch):
    before = seed(store)
    original = setup_models._selection

    def racing_selection(*args):
        staged = original(*args)
        model_roles.save(
            store, model_roles.Update(expected_revision=1, configuration=before["configuration"])
        )
        return staged

    monkeypatch.setattr(setup_models, "_selection", racing_selection)
    router = Router()
    with pytest.raises(setup_model_probes.ProbeError) as failure:
        setup_models.activate(store, router, activation())
    assert failure.value.code == "revision_conflict"
    assert router.local.calls == []
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM setup_model_probe_requests").fetchone()[0] == 0


def test_receipt_write_failure_rolls_back_selection_and_blocks_automatic_replay(store):
    before = seed(store)
    with store._connect() as db:
        db.execute(
            "CREATE TRIGGER fail_probe BEFORE INSERT ON setup_model_probe_receipts BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END"
        )
    router = Router()
    with pytest.raises(sqlite3.IntegrityError):
        setup_models.activate(store, router, activation())
    assert model_roles.load(store) == before
    with pytest.raises(setup_model_probes.ProbeError) as failure:
        setup_models.activate(store, router, activation())
    assert failure.value.code == "probe_not_replayable"
    assert setup_model_probes.current(store, 2, "spare") is None
    assert len(router.local.calls) == 1


def test_probe_identity_cannot_be_reused_as_activation(store):
    seed(store)
    router = Router()
    setup_model_probes.probe(
        store,
        router,
        setup_model_probes.ProbeInput(
            requestId="shared-request", candidateId="working", expectedRevision=1
        ),
    )
    with pytest.raises(setup_model_probes.ProbeError) as failure:
        setup_models.activate(
            store, router, activation("shared-request", revision=0, candidate="working")
        )
    assert failure.value.code == "replay_conflict"
    assert len(router.local.calls) == 1


def test_legacy_probe_request_migration_retains_identity_and_receipt(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            setup_model_probes.SCHEMA.replace(
                ",\n    operation              TEXT NOT NULL DEFAULT 'probe'", ""
            )
        )
        now = datetime.now(UTC).timestamp()
        db.execute(
            "INSERT INTO setup_model_probe_requests VALUES(?,?,?,?,?,?,?)",
            ("old-probe", 1, "working", "passed", None, now, now),
        )
        original = (
            "old-probe",
            1,
            "working",
            "ollama",
            "working:3b",
            "working:3b",
            "local",
            "a" * 64,
            now,
            now,
        )
        db.execute("INSERT INTO setup_model_probe_receipts VALUES(?,?,?,?,?,?,?,?,?,?)", original)
    with closing(Store(path)) as store:
        with store._connect() as db:
            assert (
                db.execute("SELECT operation FROM setup_model_probe_requests").fetchone()[0]
                == "probe"
            )
            assert (
                tuple(db.execute("SELECT * FROM setup_model_probe_receipts").fetchone()) == original
            )
        assert setup_model_probes.current(store, 1, "working").requestId == "old-probe"
