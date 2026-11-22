"""POST /inference service keys, the Ollama adapter's output options and Pi's startup checks."""

import json

import pytest
from service_inference_helpers import MEMORYGATE, OWNER, TOOLGATE, digest

from pi.api import server as api
from pi.models import inference_control, providers, service_inference


def test_service_keys_come_from_a_json_object_of_hashes():
    one, two = digest("one"), digest("two")
    assert service_inference.key_hashes("") == {}
    assert service_inference.key_hashes("  {}  ") == {}
    assert service_inference.key_hashes(
        json.dumps({"memorygate": one.upper(), "toolgate": two})
    ) == {"memorygate": one, "toolgate": two}
    for raw in (
        "not json",
        "[]",
        json.dumps({"systemgate": one}),
        json.dumps({"memorygate": "abc"}),
        json.dumps({"memorygate": 5}),
        json.dumps({"memorygate": one, "toolgate": one}),
    ):
        with pytest.raises(ValueError):
            service_inference.key_hashes(raw)


def test_a_key_names_its_service_only():
    hashes = {"memorygate": digest(MEMORYGATE), "toolgate": digest(TOOLGATE)}
    assert service_inference.authenticate(hashes, MEMORYGATE) == "memorygate"
    assert service_inference.authenticate(hashes, TOOLGATE) == "toolgate"
    assert service_inference.authenticate(hashes, digest(MEMORYGATE)) is None
    assert service_inference.authenticate(hashes, None) is None
    assert service_inference.authenticate({"memorygate": digest("")}, "") is None


@pytest.mark.parametrize("stream", [False, True])
def test_ollama_applies_the_requested_output_and_nothing_otherwise(monkeypatch, stream):
    import httpx

    sent = []
    final = {"model": "qwen3:4b", "message": {"content": "{}"}, "done": True}

    def post(url, **kwargs):
        sent.append(kwargs["json"])
        return httpx.Response(200, json=final, request=httpx.Request("POST", url))

    class Streamed:
        def __init__(self, payload):
            sent.append(payload)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def raise_for_status(self):
            pass

        def iter_lines(self):
            yield json.dumps(final)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "stream", lambda method, url, **kwargs: Streamed(kwargs["json"]))
    monkeypatch.setattr(
        httpx.Client, "stream", lambda self, method, url, **kwargs: Streamed(kwargs["json"])
    )
    adapter = providers.OllamaProvider("http://127.0.0.1:1")

    def complete():
        if not stream:
            return adapter.complete_bounded([], model="qwen3:4b", timeout=5)
        with inference_control.bind(inference_control.Token()):
            return adapter.complete_bounded([], model="qwen3:4b", timeout=5)

    complete()
    with providers.output(providers.Output(json=True, max_tokens=64, think=False)):
        complete()
    with providers.output(providers.Output(max_tokens=32)):
        complete()
    plain, wanted, limited = sent
    assert {"format", "options", "think"}.isdisjoint(plain)
    assert wanted["format"] == "json" and wanted["options"] == {"num_predict": 64}
    assert wanted["think"] is False
    assert "format" not in limited and "think" not in limited
    assert limited["options"] == {"num_predict": 32}
    assert providers._output.get() is None


def start(monkeypatch, tmp_path, keys):
    import asyncio
    import os

    import httpx

    from pi.api import startup

    for name in list(os.environ):
        if name.startswith("PI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("PI_ADMIN_KEY", "synthetic-admin-key-only")
    monkeypatch.setenv("PI_OWNER_KEY_SHA256", digest(OWNER))
    monkeypatch.setenv("PI_DB_PATH", str(tmp_path / "startup.db"))
    monkeypatch.setenv("PI_SERVICE_KEYS_SHA256", keys)

    def no_network(*args, **kwargs):
        pytest.fail("Startup must not contact external services")

    monkeypatch.setattr(httpx, "post", no_network)
    monkeypatch.setattr(httpx, "get", no_network)
    seen = {}

    async def run():
        async with startup.lifespan(api.app):
            seen.update(api.app.state.service_key_hashes)

    asyncio.run(run())
    return seen


def test_startup_reads_the_service_keys(monkeypatch, tmp_path):
    keys = {"memorygate": digest(MEMORYGATE), "toolgate": digest(TOOLGATE)}
    assert start(monkeypatch, tmp_path, json.dumps(keys)) == keys


@pytest.mark.parametrize(
    "keys",
    [
        "not json",
        json.dumps({"systemgate": digest(MEMORYGATE)}),
        json.dumps({"memorygate": digest(OWNER)}),
        json.dumps({"toolgate": digest("synthetic-admin-key-only")}),
    ],
)
def test_startup_refuses_bad_or_reused_service_keys(monkeypatch, tmp_path, keys):
    with pytest.raises(RuntimeError):
        start(monkeypatch, tmp_path, keys)
    # Refused before anything was opened.
    assert not (tmp_path / "startup.db").exists()
