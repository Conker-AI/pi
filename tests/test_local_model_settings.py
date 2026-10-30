"""The owner's local model settings reach every Ollama request, kept to safe ranges."""

import hashlib
import json
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.conversation.store import Store
from pi.models import local_settings, providers
from pi.owner import settings as owner_settings
from pi.turns import inference_control

FINAL = {"model": "qwen3:4b", "message": {"content": "{}"}, "done": True}
SERVICE_KEY = "memorygate-key-synthetic"
IDS = {
    "keep": local_settings.KEEP_LOADED,
    "context": local_settings.CONTEXT,
    "threads": local_settings.THREADS,
}


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """One store for the module: a store takes seconds to set up, and each test saves anew."""
    with closing(Store(tmp_path_factory.mktemp("local-models") / "pi.db")) as value:
        yield value


def choose(store, **values):
    """Save these settings as the whole document; a setting left out is unset."""
    revision = owner_settings.load(store)["revision"]
    owner_settings.save(
        store,
        owner_settings.Update(
            expected_revision=revision, values={IDS[name]: v for name, v in values.items()}
        ),
    )


@pytest.fixture
def sent(monkeypatch):
    """Every payload sent to Ollama, plain or streamed, on a host with 8 cores."""
    payloads = []

    def post(url, **kwargs):
        payloads.append(kwargs["json"])
        return httpx.Response(200, json=FINAL, request=httpx.Request("POST", url))

    class Streamed:
        def __init__(self, payload):
            payloads.append(payload)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def raise_for_status(self):
            pass

        def iter_lines(self):
            yield json.dumps(FINAL)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "stream", lambda method, url, **kwargs: Streamed(kwargs["json"]))
    monkeypatch.setattr(
        httpx.Client, "stream", lambda self, method, url, **kwargs: Streamed(kwargs["json"])
    )
    for name in local_settings.SERVER_CONTEXT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(local_settings, "host_cores", lambda: 8)
    return payloads


def complete(adapter, stream=False):
    if not stream:
        return adapter.complete_bounded([], model="qwen3:4b", timeout=5)
    with inference_control.bind(inference_control.Token()):
        return adapter.complete_bounded([], model="qwen3:4b", timeout=5)


@pytest.mark.parametrize("stream", [False, True])
def test_each_chosen_setting_reaches_the_request(store, sent, stream):
    choose(store, keep=30, context="16384", threads=4)
    complete(providers.OllamaProvider("http://127.0.0.1:1", store=store), stream)
    (payload,) = sent
    assert payload["stream"] is stream
    assert payload["keep_alive"] == "30m"
    assert payload["options"] == {"num_ctx": 16384, "num_thread": 4}


def test_until_restart_keeps_the_model_loaded(store, sent):
    choose(store, keep="until_restart")
    complete(providers.OllamaProvider("http://127.0.0.1:1", store=store))
    (payload,) = sent
    assert payload["keep_alive"] == -1 and "options" not in payload


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"keep": "soon", "context": True, "threads": None},
        {"keep": [], "context": "big", "threads": "auto"},
        {"keep": {"minutes": 5}, "context": "", "threads": 0},
        {"keep": "24h", "context": "-4096", "threads": -2},
    ],
)
def test_unset_or_malformed_settings_leave_ollamas_own(store, sent, values):
    choose(store, **values)
    complete(providers.OllamaProvider("http://127.0.0.1:1", store=store))
    (payload,) = sent
    assert "keep_alive" not in payload and "options" not in payload


def test_the_answer_limit_joins_the_chosen_options(store, sent):
    choose(store, context=4096, threads=2)
    with providers.output(providers.Output(json=True, max_tokens=64)):
        complete(providers.OllamaProvider("http://127.0.0.1:1", store=store))
    (payload,) = sent
    assert payload["format"] == "json"
    assert payload["options"] == {"num_ctx": 4096, "num_thread": 2, "num_predict": 64}


def test_without_a_store_nothing_is_added(store, sent):
    choose(store, keep=60, context=8192, threads=4)
    complete(providers.OllamaProvider("http://127.0.0.1:1"))
    (payload,) = sent
    assert "keep_alive" not in payload and "options" not in payload


def test_an_unreadable_settings_document_still_answers(tmp_path, sent):
    broken = Store(tmp_path / "pi.db")
    with broken._connect() as db:
        db.execute("DROP TABLE owner_settings")
    try:
        result = complete(providers.OllamaProvider("http://127.0.0.1:1", store=broken))
    finally:
        broken.close()
    (payload,) = sent
    assert result.text == "{}" and "options" not in payload


@pytest.mark.parametrize(
    "value,expected",
    [
        (5, "5m"),
        (90, "90m"),
        ("90", "90m"),
        (1440, "1440m"),
        (0, "5m"),
        (1, "5m"),
        (-3, "5m"),
        (100000, "1440m"),
        (30.9, "30m"),
        ("until_restart", -1),
        ("24h", None),
        (True, None),
        (float("nan"), None),
        (None, None),
    ],
)
def test_keep_loaded_is_kept_to_five_minutes_through_a_day(value, expected):
    assert local_settings.keep_alive(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("4096", 4096),
        (8192, 8192),
        ("32768", 32768),
        (1024, 2048),
        (65536, 32768),
        (8192.7, 8192),
        ("lots", None),
        (False, None),
        (None, None),
    ],
)
def test_context_is_kept_to_a_safe_range(value, expected):
    assert local_settings.num_ctx(value, environ={}) == expected


@pytest.mark.parametrize("name", local_settings.SERVER_CONTEXT)
def test_context_never_passes_the_servers_context_length(name):
    environ = {name: "8192"}
    assert local_settings.server_context(environ) == 8192
    assert local_settings.num_ctx("16384", environ=environ) == 8192
    assert local_settings.num_ctx("4096", environ=environ) == 4096
    assert local_settings.num_ctx(None, environ=environ) is None


def test_pis_own_context_length_wins_and_a_malformed_one_is_ignored():
    both = {"PI_OLLAMA_CONTEXT_LENGTH": "4096", "OLLAMA_CONTEXT_LENGTH": "16384"}
    assert local_settings.server_context(both) == 4096
    for malformed in ("lots", "0", "-1", "", "8k"):
        assert local_settings.server_context({"PI_OLLAMA_CONTEXT_LENGTH": malformed}) is None
        assert local_settings.num_ctx(65536, environ={"PI_OLLAMA_CONTEXT_LENGTH": malformed}) == (
            local_settings.MAX_CONTEXT
        )


def test_the_servers_context_length_caps_a_request(store, sent, monkeypatch):
    monkeypatch.setenv("PI_OLLAMA_CONTEXT_LENGTH", "8192")
    choose(store, context="32768")
    complete(providers.OllamaProvider("http://127.0.0.1:1", store=store))
    (payload,) = sent
    assert payload["options"] == {"num_ctx": 8192}


@pytest.mark.parametrize(
    "value,expected",
    [(1, 1), (4, 4), ("6", 6), (8, 8), (64, 8), (0, None), (-2, None), (False, None), ("x", None)],
)
def test_threads_are_kept_to_the_hosts_cores(monkeypatch, value, expected):
    monkeypatch.setattr(local_settings, "host_cores", lambda: 8)
    assert local_settings.num_thread(value) == expected


def test_the_host_has_at_least_one_core():
    assert local_settings.host_cores() >= 1


class Router:
    def __init__(self, local):
        self.local, self.local_model, self.providers = local, "qwen3:4b", {}

    def adapters(self):
        return {self.local.name: self.local}


def test_service_inference_uses_the_same_settings(store, sent, monkeypatch):
    choose(store, keep=120, context="8192", threads=3)
    local = providers.OllamaProvider("http://127.0.0.1:1", store=store)
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "router", Router(local), raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin-key", raising=False)
    monkeypatch.setattr(
        api.app.state,
        "service_key_hashes",
        {"memorygate": hashlib.sha256(SERVICE_KEY.encode()).hexdigest()},
        raising=False,
    )
    response = TestClient(api.app).post(
        "/inference",
        json={
            "role": "memory-sorting",
            "system": "Return JSON.",
            "prompt": "Sort this synthetic note",
            "max_tokens": 160,
            "json": True,
            "request_id": "memorygate:sort:local-0001",
        },
        headers={"X-Pi-Service-Key": SERVICE_KEY},
    )
    assert response.status_code == 200, response.text
    (payload,) = sent
    assert payload["keep_alive"] == "120m" and payload["format"] == "json"
    assert payload["options"] == {"num_ctx": 8192, "num_thread": 3, "num_predict": 160}
