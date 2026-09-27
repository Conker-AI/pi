import hashlib
from contextlib import closing

from fastapi.testclient import TestClient

from pi import agents, api, model_roles, setup_choices, setup_model_probes, setup_status
from pi.browser_contract import owner_allowed, runtime_allowed
from pi.providers import Completion
from pi.store import Store

OWNER = "setup-owner-key-" + "o" * 32


class Memory:
    def __init__(self, status="ok", configured=True):
        self.client = object() if configured else None
        self._status = status

    def health(self):
        return {"status": self._status}


class Adapter:
    def __init__(self, status="ok"):
        self.status = status

    def health(self):
        return {"status": self.status}

    def complete_bounded(self, messages, *, model, timeout):
        return Completion("ready", model, "provider")


class Router:
    def __init__(self, adapter=None):
        self.adapter = adapter

    def adapters(self):
        return {"provider": self.adapter} if self.adapter is not None else {}


class ToolGate:
    def __init__(self, status="ok", count=1, digest="1" * 64):
        self.status, self.count, self.digest = status, count, digest

    def health(self):
        return {"status": self.status}

    def tools(self):
        return [object() for _ in range(self.count)]

    def policy_summary(self):
        if self.status != "ok":
            raise RuntimeError("unavailable")
        return {"lockdown": False, "scopePatterns": ["tool:*"], "tools": [], "digest": self.digest}


class Speech:
    def __init__(self, input_status="configured", output_status="configured"):
        self.input_status = input_status
        self.output_status = output_status

    def capabilities(self):
        return {
            "stt": {"status": self.input_status},
            "tts": {"status": self.output_status},
        }


def configuration():
    disabled = {
        "enabled": False,
        "eligibleModelIds": [],
        "modelId": None,
        "timeoutMs": 1000,
        "failure": "stop",
        "fallbackModelId": None,
    }
    return model_roles.Configuration.model_validate(
        {
            "providers": [{"id": "provider", "name": "Provider", "enabled": True}],
            "models": [
                {
                    "id": "answer-model",
                    "providerId": "provider",
                    "name": "Answer model",
                    "route": "answer-model",
                    "enabled": True,
                    "routingDescription": "",
                }
            ],
            "defaultModelId": "answer-model",
            "roleSettings": {
                "answerMode": "manual",
                "roles": {
                    **{role: dict(disabled) for role in model_roles.ROLES},
                    "answer": {
                        **disabled,
                        "enabled": True,
                        "eligibleModelIds": ["answer-model"],
                        "modelId": "answer-model",
                    },
                },
            },
        }
    )


def verify_model(store, router, request_id):
    return setup_model_probes.probe(
        store,
        router,
        setup_model_probes.ProbeInput(
            requestId=request_id,
            candidateId="answer-model",
            expectedRevision=1,
        ),
    )


def choose_optional(store, step, choice, request_id):
    return setup_choices.record(
        store,
        step,
        setup_choices.ChoiceInput(
            requestId=request_id,
            choice=choice,
            expectedRevision=0,
        ),
    )


def test_default_projection_is_ordered_truthful_and_restart_stable(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        first = setup_status.load(
            store,
            owner_key_configured=False,
            memory=Memory(configured=False),
            toolgate=None,
            router=Router(),
        ).model_dump(mode="json")
    with closing(Store(path)) as store:
        second = setup_status.load(
            store,
            owner_key_configured=False,
            memory=Memory(configured=False),
            toolgate=None,
            router=Router(),
        ).model_dump(mode="json")

    assert first["schemaVersion"] == 1
    assert first["workflow"] == "first-run"
    assert first["state"] == "blocked"
    assert first["currentStep"] == "security"
    assert first["recommendedNextOperation"] == "configure_owner_channel"
    assert [step["id"] for step in first["steps"]] == [
        "security",
        "companion",
        "model",
        "memory",
        "capabilities",
        "boundaries",
        "protection",
        "rehearsal",
    ]
    states = {step["id"]: step["state"] for step in first["steps"]}
    assert states == {
        "security": "blocked",
        "companion": "in_progress",
        "model": "not_started",
        "memory": "not_started",
        "capabilities": "not_started",
        "boundaries": "not_started",
        "protection": "not_started",
        "rehearsal": "not_started",
    }
    assert first["steps"][0]["prerequisites"] == []
    assert first["steps"][0]["blockingReasonCode"] == "owner_channel_not_configured"
    assert first["steps"][1]["blockingReasonCode"] == "companion_configuration_unreviewed"
    assert first["steps"][2]["prerequisites"] == ["security", "companion"]
    first.pop("generatedAt")
    second.pop("generatedAt")
    assert first == second


def test_persisted_revisions_and_live_checks_drive_status(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        companion = agents.get(store, "companion")["configuration"]
        companion["role"] = "Owner-configured daily companion"
        agents.update(
            store,
            "companion",
            agents.UpdateAgent(
                expected_revision=1,
                configuration=agents.AgentInput.model_validate(companion),
            ),
        )
        model_roles.save(
            store, model_roles.Update(expected_revision=0, configuration=configuration())
        )
        router = Router(Adapter())
        verify_model(store, router, "setup-model-status-complete")
        choose_optional(store, "memory", "include", "setup-memory-status-include")
        choose_optional(store, "capabilities", "include", "setup-capabilities-status-include")
        result = setup_status.load(
            store,
            owner_key_configured=True,
            memory=Memory(),
            toolgate=ToolGate(count=2),
            router=router,
            speech=Speech(),
        ).model_dump(mode="json")

    steps = {step["id"]: step for step in result["steps"]}
    assert steps["security"]["state"] == "complete"
    assert steps["companion"]["evidence"][0]["revision"] == 2
    assert steps["model"]["state"] == "complete"
    assert steps["model"]["evidence"][0]["revision"] == 1
    assert steps["memory"]["state"] == "complete"
    assert steps["capabilities"]["state"] == "complete"
    assert "2 scoped capabilities" in steps["capabilities"]["evidence"][0]["detail"]
    assert steps["capabilities"]["evidence"][-1] == {
        "source": "speech",
        "status": "ok",
        "revision": None,
        "detail": "Microphone turns are configured; speech replies are configured. Credentials remain host-owned.",
    }
    assert steps["boundaries"]["blockingReasonCode"] == "boundary_receipt_unavailable"
    assert result["currentStep"] == "boundaries"
    assert result["recommendedNextOperation"] == "review_boundaries"
    assert result["state"] == "in_progress"


def test_configured_but_unverified_dependencies_are_degraded(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_choices.record(
            store,
            "companion",
            setup_choices.ChoiceInput(
                requestId="setup-companion-degraded-accept",
                choice="accept",
                expectedRevision=0,
            ),
        )
        model_roles.save(
            store, model_roles.Update(expected_revision=0, configuration=configuration())
        )
        router = Router(Adapter())
        verify_model(store, router, "setup-model-status-skips")
        choose_optional(store, "memory", "include", "setup-memory-degraded-include")
        choose_optional(store, "capabilities", "include", "setup-capabilities-degraded-include")
        result = setup_status.load(
            store,
            owner_key_configured=True,
            memory=Memory(status="unavailable"),
            toolgate=ToolGate(status="degraded"),
            router=Router(Adapter(status="unavailable")),
        )
    states = {step.id: step.state for step in result.steps}
    assert states["model"] == "degraded"
    assert states["memory"] == "degraded"
    assert states["capabilities"] == "degraded"
    assert result.currentStep == "model"
    assert result.recommendedNextOperation == "repair_model_provider"
    assert result.state == "degraded"


def test_explicit_optional_skips_advance_to_required_boundaries(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_choices.record(
            store,
            "companion",
            setup_choices.ChoiceInput(
                requestId="setup-companion-accept", choice="accept", expectedRevision=0
            ),
        )
        model_roles.save(
            store, model_roles.Update(expected_revision=0, configuration=configuration())
        )
        router = Router(Adapter())
        verify_model(store, router, "setup-model-status-optional-skips")
        setup_choices.record(
            store,
            "memory",
            setup_choices.ChoiceInput(
                requestId="setup-memory-skip", choice="skip", expectedRevision=0
            ),
        )
        setup_choices.record(
            store,
            "capabilities",
            setup_choices.ChoiceInput(
                requestId="setup-capabilities-skip", choice="skip", expectedRevision=0
            ),
        )
        result = setup_status.load(
            store,
            owner_key_configured=True,
            memory=Memory(configured=False),
            toolgate=None,
            router=router,
        )

    steps = {step.id: step for step in result.steps}
    assert steps["memory"].state == "skipped"
    assert steps["memory"].evidence[-1].revision == 1
    assert steps["capabilities"].state == "skipped"
    assert result.currentStep == "boundaries"
    assert result.recommendedNextOperation == "review_boundaries"


def test_healthy_optional_services_wait_for_owner_choice(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_choices.record(
            store,
            "companion",
            setup_choices.ChoiceInput(
                requestId="setup-companion-optional-choice",
                choice="accept",
                expectedRevision=0,
            ),
        )
        model_roles.save(
            store, model_roles.Update(expected_revision=0, configuration=configuration())
        )
        router = Router(Adapter())
        verify_model(store, router, "setup-model-optional-choice")
        result = setup_status.load(
            store,
            owner_key_configured=True,
            memory=Memory(),
            toolgate=ToolGate(count=2),
            router=router,
        )

    steps = {step.id: step for step in result.steps}
    assert steps["memory"].state == "in_progress"
    assert steps["memory"].blockingReasonCode == "memory_choice_unreviewed"
    assert steps["capabilities"].state == "in_progress"
    assert steps["capabilities"].blockingReasonCode == "capability_choice_unreviewed"
    assert result.currentStep == "memory"
    assert result.recommendedNextOperation == "configure_memory"


def test_setup_status_is_an_exact_owner_read_route(tmp_path, monkeypatch):
    store = Store(tmp_path / "pi.db")
    setup_choices.record(
        store,
        "companion",
        setup_choices.ChoiceInput(
            requestId="setup-companion-route-accept", choice="accept", expectedRevision=0
        ),
    )
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "memory", Memory(configured=False), raising=False)
    monkeypatch.setattr(api.app.state, "toolgate", None, raising=False)
    monkeypatch.setattr(api.app.state, "router", Router(), raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "admin-" + "a" * 32, raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    monkeypatch.setattr(
        api.app.state,
        "gateway_key_hash",
        hashlib.sha256(("r" * 32).encode()).hexdigest(),
        raising=False,
    )
    client = TestClient(api.app)
    try:
        assert client.get("/setup/status").status_code == 401
        assert (
            client.get("/setup/status", headers={"X-Pi-Gateway-Key": "r" * 32}).status_code == 401
        )
        response = client.get("/setup/status", headers={"X-Pi-Owner-Key": OWNER})
        assert response.status_code == 200
        assert response.json()["schemaVersion"] == 1
        assert response.json()["currentStep"] == "model"
        assert response.json()["recommendedNextOperation"] == "configure_model"
    finally:
        store.close()

    assert owner_allowed("GET", "/setup/status")
    assert not owner_allowed("POST", "/setup/status")
    assert not owner_allowed("GET", "/setup/status/anything")
    assert not runtime_allowed("GET", "/setup/status")


def test_model_activation_is_an_exact_owner_write_route(tmp_path, monkeypatch):
    store = Store(tmp_path / "pi.db")
    model_roles.save(store, model_roles.Update(expected_revision=0, configuration=configuration()))
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "memory", Memory(configured=False), raising=False)
    monkeypatch.setattr(api.app.state, "toolgate", None, raising=False)
    monkeypatch.setattr(api.app.state, "router", Router(Adapter()), raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "admin-" + "a" * 32, raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    monkeypatch.setattr(
        api.app.state,
        "gateway_key_hash",
        hashlib.sha256(("r" * 32).encode()).hexdigest(),
        raising=False,
    )
    client = TestClient(api.app)
    body = {
        "requestId": "setup-model-api-activation",
        "candidateId": "answer-model",
        "expectedRevision": 1,
    }
    try:
        assert client.post("/setup/models/activate", json=body).status_code == 401
        assert (
            client.post(
                "/setup/models/activate", json=body, headers={"X-Pi-Gateway-Key": "r" * 32}
            ).status_code
            == 401
        )
        response = client.post(
            "/setup/models/activate", json=body, headers={"X-Pi-Owner-Key": OWNER}
        )
        assert response.status_code == 200
        assert response.json()["revision"] == 2
        assert response.json()["probe"]["configurationRevision"] == 2
        assert "ready" not in response.text
    finally:
        store.close()

    assert owner_allowed("POST", "/setup/models/activate")
    assert owner_allowed("POST", "/setup/models/probe")
    assert not owner_allowed("GET", "/setup/models/activate")
    assert not owner_allowed("POST", "/setup/models/activate/anything")
    assert not runtime_allowed("POST", "/setup/models/activate")
