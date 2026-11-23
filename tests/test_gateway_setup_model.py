"""Gateway setup-control: the setup model choice, its probe, activation and the CLI that drives it."""

from __future__ import annotations

import json
import sys

import httpx
import pytest

from gateway.setup_control import (
    SetupControlError,
    activate_setup_model,
    model_options,
    probe_setup_model,
    set_setup_model,
)
from tests.gateway_setup_helpers import config


def test_setup_model_uses_server_candidates_and_current_revision():
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "revision": 4,
                    "candidates": [
                        {
                            "id": "local-answer",
                            "providerId": "ollama",
                            "providerName": "Local model",
                            "name": "qwen3:4b",
                            "route": "qwen3:4b",
                            "status": "ready",
                            "selected": False,
                            "execution": "local",
                            "dataNotice": "The setup test stays on this server.",
                            "costNotice": "No provider charge.",
                        }
                    ],
                },
            )
        body = json.loads(request.content)
        assert body == {"candidateId": "local-answer", "expectedRevision": 4}
        return httpx.Response(
            200,
            json={"revision": 5, "configuration": {"defaultModelId": "local-answer"}},
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        assert model_options(config(), client)["revision"] == 4
        saved = set_setup_model(config(), "local-answer", client)

    assert saved["revision"] == 5
    assert [request.url.path for request in seen] == [
        "/setup/models",
        "/setup/models",
        "/setup/models",
    ]
    assert all(request.headers["x-pi-owner-key"] == config().pi_owner_key for request in seen)


def test_setup_model_probe_is_revision_bound_and_owner_only():
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "schemaVersion": 1,
                "requestId": body["requestId"],
                "configurationRevision": body["expectedRevision"],
                "candidateId": body["candidateId"],
                "providerId": "ollama",
                "requestedModel": "qwen3:4b",
                "actualModel": "qwen3:4b",
                "execution": "local",
                "responseDigest": "a" * 64,
                "completedAt": "2026-09-27T00:00:00Z",
                "recordedAt": "2026-09-27T00:00:00Z",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        saved = probe_setup_model(config(), "local-answer", 5, client)

    assert saved["configurationRevision"] == 5
    assert seen[0].url.path == "/setup/models/probe"
    assert seen[0].headers["x-pi-owner-key"] == config().pi_owner_key


def test_activate_setup_model_uses_one_verified_owner_write():
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "revision": 4,
                    "candidates": [
                        {
                            "id": "hosted-answer",
                            "providerId": "openai",
                            "providerName": "OpenAI",
                            "name": "Answer",
                            "route": "answer",
                            "status": "unverified",
                            "selected": False,
                            "execution": "hosted",
                            "dataNotice": "The setup test is sent to OpenAI.",
                            "costNotice": "Provider billing may apply.",
                        }
                    ],
                },
            )
        body = json.loads(request.content)
        assert request.url.path == "/setup/models/activate"
        assert body["candidateId"] == "hosted-answer" and body["expectedRevision"] == 4
        return httpx.Response(
            200,
            json={
                "revision": 5,
                "candidateId": "hosted-answer",
                "probe": {
                    "schemaVersion": 1,
                    "requestId": body["requestId"],
                    "configurationRevision": 5,
                    "candidateId": "hosted-answer",
                    "providerId": "openai",
                    "requestedModel": "answer",
                    "actualModel": "answer-2026",
                    "execution": "hosted",
                    "responseDigest": "b" * 64,
                    "completedAt": "2026-09-27T00:00:00Z",
                    "recordedAt": "2026-09-27T00:00:00Z",
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        saved = activate_setup_model(config(), "hosted-answer", client)

    assert saved["revision"] == 5
    assert [request.url.path for request in seen] == ["/setup/models", "/setup/models/activate"]


def test_gateway_cli_dispatches_model_activation(monkeypatch, capsys):
    from gateway import __main__ as cli
    from gateway import setup_control

    calls = []

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(sys, "argv", ["gateway", "set-setup-model", "local-answer"])
    monkeypatch.setattr("gateway.api.Config.environment", classmethod(lambda _cls: config()))
    monkeypatch.setattr("gateway.api.Config.validate", lambda _self: None)
    monkeypatch.setattr("httpx.Client", Client)
    monkeypatch.setattr(
        setup_control,
        "activate_setup_model",
        lambda _config, candidate, _client: {
            "revision": 1,
            "candidateId": candidate,
            "probe": {"configurationRevision": 1},
        },
    )

    cli.main()

    assert json.loads(capsys.readouterr().out)["candidateId"] == "local-answer"
    assert calls == [{"trust_env": False, "follow_redirects": False, "timeout": 75}]


def test_setup_model_refuses_unknown_candidate_without_write():
    calls = []

    def upstream(request: httpx.Request):
        calls.append(request)
        return httpx.Response(200, json={"revision": 0, "candidates": []})

    with (
        httpx.Client(transport=httpx.MockTransport(upstream)) as client,
        pytest.raises(SetupControlError, match="candidate ID"),
    ):
        set_setup_model(config(), "invented-model", client)
    assert len(calls) == 1 and calls[0].method == "GET"
