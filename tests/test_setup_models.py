from contextlib import closing

import pytest

from pi import model_roles, setup_model_probes, setup_models
from pi.providers import Completion, ProviderUnavailable
from pi.store import Store


class Adapter:
    name = "ollama"

    def __init__(self, status="ok"):
        self.status = status
        self.calls = []

    def health(self):
        return {"status": self.status}

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append((messages, model, timeout))
        if self.status == "failure":
            raise ProviderUnavailable("synthetic")
        return Completion("ready", model, self.name)


class Router:
    def __init__(self, status="ok", route="qwen3:4b"):
        self.local = Adapter(status)
        self.local_model = route

    def adapters(self):
        return {self.local.name: self.local}


def test_fresh_setup_discovers_and_selects_the_live_local_model(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        found = setup_models.options(store, Router())
        assert found.revision == 0
        assert [item.id for item in found.candidates] == ["local-answer"]
        assert found.candidates[0].route == "qwen3:4b"
        assert found.candidates[0].status == "ready"

        saved = setup_models.select(
            store,
            Router(),
            setup_models.Selection(candidateId="local-answer", expectedRevision=0),
        )

        assert saved["revision"] == 1
        assert saved["configuration"]["defaultModelId"] == "local-answer"
        answer = saved["configuration"]["roleSettings"]["roles"]["answer"]
        assert answer["enabled"] and answer["modelId"] == "local-answer"
        assert set(saved["configuration"]["roleSettings"]["roles"]) == set(model_roles.ROLES)


def test_selection_refuses_unhealthy_or_stale_candidates(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        with pytest.raises(RuntimeError, match="not ready"):
            setup_models.select(
                store,
                Router("not_configured"),
                setup_models.Selection(candidateId="local-answer", expectedRevision=0),
            )
        with pytest.raises(ValueError, match="changed"):
            setup_models.select(
                store,
                Router(),
                setup_models.Selection(candidateId="local-answer", expectedRevision=1),
            )


def test_existing_advanced_roles_survive_answer_selection(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_models.select(
            store,
            Router(route="first:1b"),
            setup_models.Selection(candidateId="local-answer", expectedRevision=0),
        )
        configuration = model_roles.load(store)["configuration"]
        configuration["roleSettings"]["roles"]["proposals"]["timeoutMs"] = 54321
        model_roles.save(
            store,
            model_roles.Update(expected_revision=1, configuration=configuration),
        )

        found = setup_models.options(store, Router(route="second:2b"))
        new_candidate = next(item for item in found.candidates if item.route == "second:2b")
        saved = setup_models.select(
            store,
            Router(route="second:2b"),
            setup_models.Selection(candidateId=new_candidate.id, expectedRevision=2),
        )

        assert saved["configuration"]["roleSettings"]["roles"]["proposals"]["timeoutMs"] == 54321
        assert (
            saved["configuration"]["roleSettings"]["roles"]["answer"]["modelId"] == new_candidate.id
        )


def test_probe_is_revision_bound_idempotent_and_stores_no_response_text(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        router = Router()
        setup_models.select(
            store,
            router,
            setup_models.Selection(candidateId="local-answer", expectedRevision=0),
        )
        body = setup_model_probes.ProbeInput(
            requestId="setup-model-probe-one",
            candidateId="local-answer",
            expectedRevision=1,
        )
        first = setup_model_probes.probe(store, router, body)
        second = setup_model_probes.probe(store, router, body)

        assert first == second
        assert first.execution == "local"
        assert first.actualModel == "qwen3:4b"
        assert len(first.responseDigest) == 64
        assert len(router.local.calls) == 1
        messages, route, timeout = router.local.calls[0]
        assert [message.content for message in messages] == [setup_model_probes.PROBE_PROMPT]
        assert route == "qwen3:4b"
        assert timeout == setup_model_probes.PROBE_TIMEOUT_SECONDS
        with store._connect() as db:
            values = db.execute(
                "SELECT request_id, response_digest FROM setup_model_probe_receipts"
            ).fetchone()
        assert tuple(values) == (body.requestId, first.responseDigest)
        assert "ready" not in repr(tuple(values))


def test_probe_failure_is_not_retried_or_recorded_as_success(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        healthy = Router()
        setup_models.select(
            store,
            healthy,
            setup_models.Selection(candidateId="local-answer", expectedRevision=0),
        )
        failing = Router(status="failure")
        body = setup_model_probes.ProbeInput(
            requestId="setup-model-probe-failure",
            candidateId="local-answer",
            expectedRevision=1,
        )
        with pytest.raises(setup_model_probes.ProbeError, match="did not answer"):
            setup_model_probes.probe(store, failing, body)
        with pytest.raises(setup_model_probes.ProbeError, match="already failed"):
            setup_model_probes.probe(store, failing, body)
        assert len(failing.local.calls) == 1
        assert setup_model_probes.current(store, 1, "local-answer") is None


def test_probe_rejects_stale_configuration_and_changed_selection(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        router = Router()
        setup_models.select(
            store,
            router,
            setup_models.Selection(candidateId="local-answer", expectedRevision=0),
        )
        with pytest.raises(setup_model_probes.ProbeError, match="changed"):
            setup_model_probes.probe(
                store,
                router,
                setup_model_probes.ProbeInput(
                    requestId="setup-model-probe-stale",
                    candidateId="local-answer",
                    expectedRevision=2,
                ),
            )


def test_activation_selects_and_proves_model_in_one_owner_operation(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        router = Router()
        activated = setup_models.activate(
            store,
            router,
            setup_models.Activation(
                requestId="setup-model-activation-one",
                candidateId="local-answer",
                expectedRevision=0,
            ),
        )

        assert activated.revision == 1
        assert activated.candidateId == "local-answer"
        assert activated.probe.configurationRevision == 1
        status = setup_model_probes.current(store, 1, "local-answer")
        assert status == activated.probe
