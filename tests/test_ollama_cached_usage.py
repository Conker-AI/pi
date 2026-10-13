"""Reported Ollama cache usage reaches receipts without guessed zeroes or prices."""

import json
from contextlib import closing, contextmanager, nullcontext

import httpx
import pytest

from pi import live_stream, research_usage, session_settings
from pi.providers import Message, OllamaProvider
from pi.store import Store


@pytest.mark.parametrize("stream", [False, True], ids=["json", "final-stream-chunk"])
@pytest.mark.parametrize(
    "reported,expected",
    [
        ({}, None),
        ({"prompt_eval_cached_count": None}, None),
        ({"prompt_eval_cached_count": 0}, 0),
        ({"prompt_eval_cached_count": 529}, 529),
        ({"prompt_eval_cached_count": -1}, None),
        ({"prompt_eval_cached_count": True}, None),
        ({"prompt_eval_cached_count": 3.0}, None),
        ({"prompt_eval_cached_count": "529"}, None),
        ({"prompt_eval_cached_count": []}, None),
        ({"prompt_eval_cached_count": float("nan")}, None),
        ({"prompt_eval_cached_count": float("inf")}, None),
        ({"prompt_eval_cached_count": 2**31}, None),
    ],
)
def test_reported_cache_tokens_survive_completion_and_usage_ledger(
    tmp_path, monkeypatch, stream, reported, expected
):
    final = {
        "model": "synthetic-local",
        "message": {"role": "assistant", "content": "Answer"},
        "done": True,
        "prompt_eval_count": 530,
        "eval_count": 10,
        **reported,
    }
    calls = []

    def post(url, **kwargs):
        assert not stream
        calls.append(kwargs["json"])
        return httpx.Response(200, content=json.dumps(final), request=httpx.Request("POST", url))

    @contextmanager
    def chunks(method, url, **kwargs):
        assert stream and method == "POST"
        calls.append(kwargs["json"])
        # Intermediate fields cannot replace missing/invalid final usage.
        body = json.dumps(
            {"message": {"content": "Ans"}, "done": False, "prompt_eval_cached_count": 77}
        )
        body += "\n" + json.dumps({**final, "message": {"content": "wer"}}) + "\n"
        yield httpx.Response(200, content=body, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "stream", chunks)
    live_stream.reset_for_tests()
    with closing(Store(tmp_path / "usage.db")) as store:
        sid = store.create_session()
        tid = store.start_turn(sid)
        execution = session_settings.execution(store, sid, tid)
        provider = research_usage.wrap(store, execution, OllamaProvider("http://127.0.0.1:11434"))
        try:
            with (
                live_stream.open_reply("cached_usage") if stream else nullcontext(),
                live_stream.answering(),
            ):
                result = provider.complete([Message("user", "Synthetic")], model="synthetic")
        finally:
            live_stream.reset_for_tests()
        assert result.text == "Answer" and result.cached_tokens == expected
        assert (result.input_tokens, result.output_tokens, result.cost_usd) == (530, 10, None)
        assert len(calls) == 1 and calls[0]["stream"] is stream
        receipt = research_usage.receipt(store, tid)
        assert receipt["foreground"]["attempt_count"] == 1
        assert receipt["attempts"][0]["cached_tokens"] == expected
        assert receipt["foreground"]["totals"]["cached_tokens"] == expected
        assert receipt["foreground"]["unknown_counts"]["cached_tokens"] == int(expected is None)
        assert receipt["foreground"]["complete"]["cached_tokens"] is (expected is not None)
        assert receipt["foreground"]["totals"]["cost_usd"] is None
        assert store.get_turn(tid)["cached_tokens"] == expected
