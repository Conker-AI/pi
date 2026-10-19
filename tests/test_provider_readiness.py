"""Catalogue observations never stand in for explicit hosted inference receipts."""

from contextlib import closing
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.conversation.store import Store
from pi.models import roles as model_roles
from pi.models.openrouter import CATALOGUE_URL, CHAT_URL, OpenRouterProvider
from pi.models.providers import Completion
from pi.models.routing import Router
from pi.setup import model_probes as setup_model_probes
from pi.setup import models as setup_models
from pi.setup import status_steps

AVAILABLE = "synthetic/available:free"
MISSING = "synthetic/not-in-catalogue"


class Local:
    name = "ollama"

    def __init__(self):
        self.calls = []

    def health(self):
        return {"status": "ok"}

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append(model)
        return Completion("ready", model, self.name)


def wire(monkeypatch, entries=None):
    seen = {"catalogue": [], "inference": []}
    if entries is None:
        entries = [
            {
                "id": AVAILABLE,
                "context_length": 4096,
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "pricing": {"prompt": "0", "completion": "0", "request": "0"},
            }
        ]

    def get(url, **kwargs):
        assert url == CATALOGUE_URL
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        seen["catalogue"].append(url)
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"data": entries},
        )

    def post(url, **kwargs):
        seen["inference"].append(url)
        pytest.fail("A readiness read must not run inference")

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    return seen


def mount(monkeypatch, hosted):
    local = Local()
    router = Router(local_provider=local, local_model="synthetic-local", hosted_provider=hosted)
    for key, value in {
        "local": local,
        "hosted": hosted,
        "router": router,
        "admin_key": "synthetic-owner-key",
        "gateway_key_hash": "",
    }.items():
        monkeypatch.setattr(api.app.state, key, value, raising=False)
    return TestClient(api.app), router


def seed(store):
    roles = {
        role: {
            "enabled": False,
            "eligibleModelIds": [],
            "modelId": None,
            "timeoutMs": 1000,
            "failure": "stop",
            "fallbackModelId": None,
        }
        for role in model_roles.ROLES
    }
    roles["answer"].update(enabled=True, eligibleModelIds=["working", "hosted"], modelId="working")
    configuration = {
        "providers": [
            {"id": "ollama", "name": "Local", "enabled": True},
            {"id": "openrouter", "name": "Hosted", "enabled": True},
        ],
        "models": [
            {
                "id": "working",
                "providerId": "ollama",
                "name": "Working",
                "route": "synthetic-local",
                "enabled": True,
            },
            {
                "id": "hosted",
                "providerId": "openrouter",
                "name": "Hosted",
                "route": AVAILABLE,
                "enabled": True,
            },
            {
                "id": "missing",
                "providerId": "openrouter",
                "name": "Absent",
                "route": MISSING,
                "enabled": True,
            },
        ],
        "defaultModelId": "working",
        "roleSettings": {"answerMode": "manual", "roles": roles},
    }
    return model_roles.save(
        store, model_roles.Update(expected_revision=0, configuration=configuration)
    )


def activation(candidate="hosted", revision=1, request="hosted_explicit_activation"):
    return setup_models.Activation(
        candidateId=candidate, expectedRevision=revision, requestId=request
    )


def test_catalogue_and_cached_readiness_are_unverified_without_inference(monkeypatch):
    seen = wire(monkeypatch)
    hosted = OpenRouterProvider("synthetic-key")
    client, _ = mount(monkeypatch, hosted)
    assert client.get("/models").status_code == 401
    response = client.get("/models", headers={"X-Pi-Key": "synthetic-owner-key"})
    assert response.status_code == 200
    assert response.json()["hosted"] == {
        "status": "unverified",
        "provider": "openrouter",
        "allow_paid": False,
        "free_models": [AVAILABLE],
        "free_count": 1,
        "total_text_models": 1,
    }
    assert hosted.health() == {"status": "unverified"}
    assert seen == {"catalogue": [CATALOGUE_URL], "inference": []}
    # A cached catalogue does not test a now-unreachable endpoint or its key.
    monkeypatch.setattr(
        httpx, "get", lambda *_a, **_k: pytest.fail("Unexpected additional network read")
    )
    assert api.models()["hosted"]["status"] == "unverified"
    assert hosted.health()["status"] == "unverified"
    assert seen["inference"] == []


@pytest.mark.parametrize("paid", [False, True])
@pytest.mark.parametrize("failure", ["connect", "http", "malformed"])
def test_configured_catalogue_failure_keeps_identity_and_paid_policy(monkeypatch, paid, failure):
    wire(monkeypatch)
    provider = OpenRouterProvider("synthetic-secret", allow_paid=paid)
    mount(monkeypatch, provider)

    def fail(url, **kwargs):
        if failure == "connect":
            raise httpx.ConnectError("synthetic-sensitive-transport-detail")
        return httpx.Response(
            503 if failure == "http" else 200,
            json={"error": "synthetic-sensitive-provider-detail"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fail)
    hosted = api.models()["hosted"]
    assert hosted == {
        "status": "unavailable",
        "provider": "openrouter",
        "allow_paid": paid,
        "reason": "ProviderUnavailable",
    }
    assert "synthetic" not in repr(hosted)


def test_absent_provider_remains_unconfigured_without_network(monkeypatch):
    wire(monkeypatch)
    mount(monkeypatch, None)
    monkeypatch.setattr(
        httpx, "get", lambda *_a, **_k: pytest.fail("No hosted provider configured")
    )
    assert api.models()["hosted"] == {"status": "not_configured"}
    assert OpenRouterProvider("").health() == {"status": "not_configured", "reason": "no API key"}


def test_empty_catalogue_models_read_observes_once_without_followup_request(monkeypatch):
    seen = wire(monkeypatch, entries=[])
    mount(monkeypatch, OpenRouterProvider("synthetic-key"))
    original = httpx.get

    def once(url, **kwargs):
        if seen["catalogue"]:
            raise httpx.ConnectError("A second readiness request would fail")
        return original(url, **kwargs)

    monkeypatch.setattr(httpx, "get", once)
    assert api.models()["hosted"] == {
        "status": "unverified",
        "provider": "openrouter",
        "allow_paid": False,
        "free_models": [],
        "free_count": 0,
        "total_text_models": 0,
    }
    assert seen == {"catalogue": [CATALOGUE_URL], "inference": []}


@pytest.mark.parametrize("paid", [False, True])
def test_paid_only_catalogue_remains_testable_without_weakening_dispatch_guard(
    monkeypatch, tmp_path, paid
):
    seen = wire(
        monkeypatch,
        entries=[
            {
                "id": AVAILABLE,
                "context_length": 4096,
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "pricing": {"prompt": "0.001", "completion": "0.002", "request": "0"},
            }
        ],
    )
    provider = OpenRouterProvider("synthetic-key", allow_paid=paid)
    _, router = mount(monkeypatch, provider)
    assert provider.health() == {"status": "unverified"}
    assert api.models()["hosted"] == {
        "status": "unverified",
        "provider": "openrouter",
        "allow_paid": paid,
        "free_models": [],
        "free_count": 0,
        "total_text_models": 1,
    }

    def response(url, **kwargs):
        assert paid is True
        seen["inference"].append(url)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [{"message": {"content": "ready"}}],
            },
        )

    monkeypatch.setattr(httpx, "post", response)
    with closing(Store(tmp_path / "models.db")) as store:
        before = seed(store)
        assert (
            next(
                row.status
                for row in setup_models.options(store, router).candidates
                if row.id == "hosted"
            )
            == "unverified"
        )
        if paid:
            result = setup_models.activate(store, router, activation())
            assert result.probe.requestedModel == AVAILABLE
        else:
            with pytest.raises(setup_model_probes.ProbeError) as failure:
                setup_models.activate(store, router, activation())
            assert failure.value.code == "provider_error"
            assert model_roles.load(store) == before
            assert setup_model_probes.current(store, 2, "hosted") is None
    assert seen == {"catalogue": [CATALOGUE_URL], "inference": [CHAT_URL] if paid else []}


def test_models_free_order_comes_from_the_same_catalogue_snapshot(monkeypatch):
    entries = [
        {
            "id": identity,
            "context_length": length,
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "pricing": {"prompt": "0", "completion": "0", "request": "0"},
        }
        for identity, length in [("small", 128), ("big", 4096), ("tie", 4096)]
    ]
    seen = wire(monkeypatch, entries)
    mount(monkeypatch, OpenRouterProvider("synthetic-key"))
    assert api.models()["hosted"]["free_models"] == ["big", "tie", "small"]
    assert seen == {"catalogue": [CATALOGUE_URL], "inference": []}


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("catalogue", "unknown"),
        ("empty", "unknown"),
        ("unavailable", "unavailable"),
        ("absent", "not_configured"),
    ],
)
def test_module_health_preserves_contract_enum_and_derived_status(monkeypatch, mode, expected):
    seen = wire(monkeypatch, entries=[] if mode == "empty" else None)
    mount(monkeypatch, None if mode == "absent" else OpenRouterProvider("synthetic-key"))
    if mode == "unavailable":

        def fail(url, **kwargs):
            seen["catalogue"].append(url)
            raise httpx.ConnectError("synthetic-private-detail")

        monkeypatch.setattr(httpx, "get", fail)
    for key, value in {
        "store": SimpleNamespace(health=lambda: {"status": "ok"}),
        "loop": SimpleNamespace(
            memory=SimpleNamespace(health=lambda: {"status": "not_configured"})
        ),
        "provider_credentials": [],
        "speech": SimpleNamespace(
            capabilities=lambda: {
                "stt": {"status": "not_configured"},
                "tts": {"status": "not_configured"},
            }
        ),
        "speech_credential_configured": False,
        "toolgate": None,
    }.items():
        monkeypatch.setattr(api.app.state, key, value, raising=False)
    monkeypatch.setattr(api, "_health_cache", {})
    result = api.health()
    assert result["checks"]["hosted_provider"]["status"] == expected
    assert {"ok", "not_configured"} == api.HEALTHY
    assert all(
        row["status"] in {"ok", "degraded", "unavailable", "not_configured", "unknown"}
        for row in result["checks"].values()
    )
    assert result["status"] == ("ok" if mode == "absent" else "degraded")
    assert result["degraded"] == ([] if mode == "absent" else ["hosted_provider"])
    if expected == "unknown":
        assert result["checks"]["hosted_provider"] == {
            "status": "unknown",
            "reason": "Configured; inference not verified",
        }
    assert "synthetic-private" not in repr(result)
    assert seen == {"catalogue": [] if mode == "absent" else [CATALOGUE_URL], "inference": []}


def test_configured_routes_remain_unverified_and_owner_selectable(monkeypatch, tmp_path):
    seen = wire(monkeypatch)
    _, router = mount(monkeypatch, OpenRouterProvider("synthetic-key"))
    with closing(Store(tmp_path / "models.db")) as store:
        seed(store)
        options = setup_models.options(store, router)
        assert {row.id: row.status for row in options.candidates} == {
            "working": "ready",
            "hosted": "unverified",
            "missing": "unverified",
        }
        # An absent catalogue entry is not an added alias/membership policy.
        saved = setup_models.select(
            store, router, setup_models.Selection(candidateId="missing", expectedRevision=1)
        )
        assert saved["configuration"]["defaultModelId"] == "missing"
        assert status_steps._model(store, router).blockingReasonCode == "model_response_unverified"
    assert seen == {"catalogue": [CATALOGUE_URL], "inference": []}


def test_explicit_unverified_activation_records_receipt_and_replay_does_not_dispatch(
    monkeypatch, tmp_path
):
    seen = wire(monkeypatch)
    hosted = OpenRouterProvider("synthetic-key")
    _, router = mount(monkeypatch, hosted)

    def response(url, **kwargs):
        assert url == CHAT_URL and kwargs["json"]["model"] == AVAILABLE
        assert kwargs["json"]["messages"] == [
            {"role": "user", "content": setup_model_probes.PROBE_PROMPT}
        ]
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        seen["inference"].append(url)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "synthetic/actual-result",
                "choices": [{"message": {"content": "ready"}}],
            },
        )

    monkeypatch.setattr(httpx, "post", response)
    with closing(Store(tmp_path / "models.db")) as store:
        before = seed(store)
        result = setup_models.activate(store, router, activation())
        assert result.revision == 2
        assert result.probe.providerId == "openrouter"
        assert result.probe.requestedModel == AVAILABLE
        assert result.probe.actualModel == "synthetic/actual-result"
        assert result.probe.execution == "hosted"
        assert status_steps._model(store, router).state == "complete"
        assert setup_model_probes.current(store, 2, "hosted") == result.probe
        later = model_roles.save(
            store, model_roles.Update(expected_revision=2, configuration=before["configuration"])
        )
        monkeypatch.setattr(
            httpx, "get", lambda *_a, **_k: pytest.fail("Replay must not read provider")
        )
        monkeypatch.setattr(
            httpx, "post", lambda *_a, **_k: pytest.fail("Replay must not invoke provider")
        )
        assert setup_models.activate(store, router, activation()) == result
        assert model_roles.load(store) == later
    assert seen == {"catalogue": [CATALOGUE_URL], "inference": [CHAT_URL]}
    assert hosted.health() == {"status": "unverified"}


def test_401_activation_preserves_previous_choice_and_evidence_without_retry(monkeypatch, tmp_path):
    seen = wire(monkeypatch)
    _, router = mount(monkeypatch, OpenRouterProvider("synthetic-rejected-key"))
    with closing(Store(tmp_path / "models.db")) as store:
        seed(store)
        working = setup_models.activate(
            store, router, activation("working", request="working_explicit_activation")
        )
        before = model_roles.load(store)

        def rejected(url, **kwargs):
            seen["inference"].append(url)
            assert model_roles.load(store) == before
            return httpx.Response(
                401, request=httpx.Request("POST", url), json={"error": "synthetic-secret-error"}
            )

        monkeypatch.setattr(httpx, "post", rejected)
        body = activation(revision=2)
        with pytest.raises(setup_model_probes.ProbeError) as error:
            setup_models.activate(store, router, body)
        assert error.value.code == "provider_unavailable"
        assert "synthetic-secret" not in str(error.value)
        assert model_roles.load(store) == before
        assert setup_model_probes.current(store, 2, "working") == working.probe
        assert setup_model_probes.current(store, 3, "hosted") is None
        with pytest.raises(setup_model_probes.ProbeError) as replay:
            setup_models.activate(store, router, body)
        assert replay.value.code == "probe_not_replayable"
        assert model_roles.load(store) == before
        assert len(router.local.calls) == 1
    assert seen == {"catalogue": [CATALOGUE_URL], "inference": [CHAT_URL]}
