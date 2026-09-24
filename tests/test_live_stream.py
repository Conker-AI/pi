"""Live answer previews: synthetic HTTP only, no network or paid request."""

import json
from contextlib import closing, contextmanager

import httpx
import pytest

from pi import live_stream
from pi.direct_providers import AnthropicProvider, OpenAIProvider
from pi.loop import Loop
from pi.openrouter import ModelInfo, ModelUnusable, OpenRouterProvider
from pi.providers import Completion, Message, OllamaProvider, ProviderUnavailable
from pi.routing import Router
from pi.store import Store


@pytest.fixture(autouse=True)
def clean():
    live_stream.reset_for_tests()
    yield
    live_stream.reset_for_tests()


def events(request_id):
    batch, finished = live_stream.read(request_id)
    return [(e["type"], e.get("text")) for e in batch], finished


def text_of(request_id):
    shown = ""
    for kind, text in events(request_id)[0]:
        shown = "" if kind == "reset" else shown + (text or "")
    return shown


# --- the preview registry ----------------------------------------------------


def test_only_answer_calls_are_previewed_and_the_reply_finishes():
    with live_stream.open_reply("r1"):
        live_stream.delta("helper output must stay private")
        with live_stream.answering():
            live_stream.begin_attempt()
            live_stream.delta("Hello ")
            live_stream.delta("world")
    assert events("r1") == (
        [("reset", None), ("delta", "Hello "), ("delta", "world"), ("done", None)],
        True,
    )


def test_tool_request_lines_never_reach_the_preview():
    with live_stream.open_reply("r2"), live_stream.answering():
        live_stream.delta("Checking your calendar.\n")
        live_stream.delta('{"tool": "calendar.')
        live_stream.delta('read", "args": {}}\n')
        live_stream.delta("Done.")
    assert text_of("r2") == "Checking your calendar.\nDone."


def test_a_line_that_merely_starts_with_a_brace_is_shown_once_complete():
    with live_stream.open_reply("r3"), live_stream.answering():
        live_stream.delta('{"a": 1')
        assert text_of("r3") == ""
        live_stream.delta("}\nafter")
    assert text_of("r3") == '{"a": 1}\nafter'


def test_a_new_attempt_replaces_the_previous_preview():
    with live_stream.open_reply("r4"), live_stream.answering():
        live_stream.begin_attempt()
        live_stream.delta("from a model that then failed")
        live_stream.begin_attempt()
        live_stream.delta("from the fallback")
    assert text_of("r4") == "from the fallback"


def test_preview_is_bounded(monkeypatch):
    monkeypatch.setattr(live_stream, "MAX_PREVIEW_CHARACTERS", 5)
    with live_stream.open_reply("r5"), live_stream.answering():
        live_stream.delta("abc")
        live_stream.delta("defgh")
    assert text_of("r5") == "abcde"


def test_without_a_request_id_nothing_is_recorded():
    with live_stream.open_reply(None), live_stream.answering():
        assert not live_stream.active()
        live_stream.delta("x")
    assert live_stream.read("None") is None


# --- providers ----------------------------------------------------------------


@contextmanager
def streamed(body: bytes, status=200):
    captured = []

    @contextmanager
    def stream(method, url, **kwargs):
        captured.append((url, kwargs))
        yield httpx.Response(status, content=body, request=httpx.Request(method, url))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx, "stream", stream)
        patch.setattr(httpx, "post", lambda *a, **k: pytest.fail("streaming answers must not POST"))
        yield captured


def sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


def run_streaming(provider, body, request_id="p", **kwargs):
    with streamed(body) as captured, live_stream.open_reply(request_id), live_stream.answering():
        result = provider.complete_bounded([Message("user", "Q")], model="m", timeout=2, **kwargs)
    return result, captured


def test_openai_stream_matches_the_non_streamed_completion():
    body = sse(
        {"model": "actual", "choices": [{"index": 0, "delta": {"content": "Ans"}}]},
        {"choices": [{"index": 0, "delta": {"content": "wer"}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 5},
            },
        },
    )
    result, captured = run_streaming(OpenAIProvider("synthetic", allow_paid=True), body)
    assert (
        result.text,
        result.model,
        result.input_tokens,
        result.output_tokens,
        result.cached_tokens,
    ) == (
        "Answer",
        "actual",
        12,
        3,
        5,
    )
    assert captured[0][1]["json"]["stream"] is True
    assert captured[0][1]["json"]["stream_options"] == {"include_usage": True}
    assert text_of("p") == "Answer"


def test_anthropic_stream_matches_the_non_streamed_completion():
    body = b"".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode()
        for e in [
            {
                "type": "message_start",
                "message": {
                    "model": "actual",
                    "usage": {
                        "input_tokens": 4,
                        "cache_creation_input_tokens": 3,
                        "cache_read_input_tokens": 5,
                    },
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Ans"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "wer"},
            },
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 3},
            },
            {"type": "message_stop"},
        ]
    )
    result, _ = run_streaming(AnthropicProvider("synthetic", allow_paid=True), body)
    assert (result.text, result.input_tokens, result.output_tokens, result.cached_tokens) == (
        "Answer",
        12,
        3,
        5,
    )
    assert result.raw == {"finish_reason": "end_turn"}
    assert text_of("p") == "Answer"


def test_ollama_stream_matches_the_non_streamed_completion():
    lines = [
        {"model": "local", "message": {"content": "Ans"}, "done": False},
        {"model": "local", "message": {"content": "wer"}, "done": False},
        {
            "model": "local",
            "message": {"content": ""},
            "done": True,
            "prompt_eval_count": 7,
            "eval_count": 2,
            "done_reason": "stop",
        },
    ]
    body = "\n".join(json.dumps(line) for line in lines).encode()
    with streamed(body) as captured, live_stream.open_reply("p"), live_stream.answering():
        result = OllamaProvider("http://ollama").complete([Message("user", "Q")], model="local")
    assert captured[0][1]["json"]["stream"] is True
    assert (result.text, result.input_tokens, result.output_tokens) == ("Answer", 7, 2)
    assert result.raw == {"done_reason": "stop"}
    assert text_of("p") == "Answer"


def openrouter():
    provider = OpenRouterProvider("synthetic", allow_paid=True)
    provider._catalogue = {"m": ModelInfo("m", 1000, 0.0, 0.0, False, {})}
    provider._fetched_at = float("inf")
    return provider


def test_openrouter_stream_keeps_reported_cost():
    body = b": OPENROUTER PROCESSING\n\n" + sse(
        {
            "model": "m",
            "choices": [{"index": 0, "delta": {"content": "Answer"}, "finish_reason": "stop"}],
        },
        {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1, "cost": 0.25}},
    )
    result, _ = run_streaming(openrouter(), body)
    assert (result.text, result.cost_usd) == ("Answer", 0.25)


def test_openrouter_refused_model_is_unusable_not_an_outage():
    with (
        streamed(b"", status=403),
        live_stream.open_reply("p"),
        live_stream.answering(),
        pytest.raises(ModelUnusable),
    ):
        openrouter().complete([Message("user", "Q")], model="m")


@pytest.mark.parametrize(
    "provider,body",
    [
        (
            lambda: OpenAIProvider("sensitive", allow_paid=True),
            b'data: {"error": {"message": "sensitive"}}\n\n',
        ),
        (
            lambda: AnthropicProvider("sensitive", allow_paid=True),
            b'data: {"type": "message_start", "message": {}}\n\n',
        ),
        (
            lambda: OllamaProvider("http://ollama"),
            b'{"message": {"content": "partial"}, "done": false}\n',
        ),
    ],
)
def test_broken_or_truncated_streams_fail_without_leaking(provider, body):
    with (
        streamed(body),
        live_stream.open_reply("p"),
        live_stream.answering(),
        pytest.raises(ProviderUnavailable) as error,
    ):
        provider().complete([Message("user", "Q")], model="m")
    assert "sensitive" not in str(error.value)


# --- the turn loop --------------------------------------------------------------


class StreamingProvider:
    name = "local"

    def complete(self, messages, *, model):
        live_stream.begin_attempt()
        for piece in ("Hello ", "owner"):
            live_stream.delta(piece)
        return Completion(text="Hello owner", model=model, provider=self.name)


def test_turn_preview_matches_the_saved_answer(tmp_path):
    with closing(Store(tmp_path / "t.db")) as store:
        sid = store.create_session()
        loop = Loop(store, Router(local_provider=StreamingProvider(), local_model="test"))
        result = loop.run_turn(sid, "Hi", request_id="turn_preview_request")
        assert result["message"]["content"] == "Hello owner"
    kinds, finished = events("turn_preview_request")
    assert finished and kinds[-1] == ("done", None)
    assert text_of("turn_preview_request") == "Hello owner"


def test_turn_without_request_id_still_works(tmp_path):
    with closing(Store(tmp_path / "t.db")) as store:
        sid = store.create_session()
        loop = Loop(store, Router(local_provider=StreamingProvider(), local_model="test"))
        assert loop.run_turn(sid, "Hi")["message"]["content"] == "Hello owner"


# --- the HTTP surface -------------------------------------------------------------


def test_stream_endpoint_replays_events_and_ends():
    from fastapi.testclient import TestClient

    from pi import api

    with live_stream.open_reply("http_request"), live_stream.answering():
        live_stream.delta("Hi")
    # Authentication is covered by the gateway tests; this checks the event stream.
    api.app.dependency_overrides[api.require_key] = lambda: None
    client = TestClient(api.app)  # no lifespan: the preview needs no store
    try:
        response = client.get("/turn-submissions/http_request/stream")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: delta" in response.text and '"text": "Hi"' in response.text
        assert response.text.rstrip().endswith('data: {"seq": 2}')
        resumed = client.get("/turn-submissions/http_request/stream?after=1")
        assert "event: delta" not in resumed.text and "event: done" in resumed.text
    finally:
        api.app.dependency_overrides.clear()
