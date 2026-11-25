"""Hide secrets from online models, end to end: what leaves Pi for each provider it builds at
startup, and what a turn keeps. Synthetic HTTP only; every secret is made up."""

import asyncio
import json
import os
import sqlite3
from contextlib import closing

import httpx
import pytest
from made_up_secrets import CARD, GITHUB_SHAPE, OPENAI_SHAPE, choose, pem
from test_tool_turns import FakeGate, Scripted

from pi.actions.toolgate import ToolResult
from pi.api import server as api
from pi.api import startup
from pi.context import research_usage
from pi.conversation.store import Store
from pi.models.providers import Message, ProviderUnavailable
from pi.models.routing import Router
from pi.turns.loop import Loop

SECRETS = (OPENAI_SHAPE, GITHUB_SHAPE, CARD, "pa55word", "FAKE" * 16)
FREE = {
    "id": "made-up/free",
    "context_length": 8000,
    "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    "pricing": {"prompt": "0", "completion": "0"},
}


def history():
    return [
        Message("system", "Memory: the owner's card is " + CARD),
        Message("user", f"Use {OPENAI_SHAPE}; my password: pa55word"),
        Message("assistant", json.dumps({"tool": "t_echo", "args": {"text": GITHUB_SHAPE}})),
        Message("tool", json.dumps({"tool": "t_echo", "result": {"text": pem()}})),
        Message("user", "Thanks."),
    ]


def routing():
    task = [{"role": "user", "content": f"Use {OPENAI_SHAPE}, card {CARD}"}]
    envelope = {"allowedModelIds": ["a", "b"], "modelDescriptions": {"a": "x", "b": "y"}}
    return [Message("system", "Route."), Message("user", json.dumps({**envelope, "task": task}))]


# provider -> (model, messages); what each one is asked.
ASKED = {
    "openrouter": ("made-up/free", history),
    "openai": ("made-up-model", history),
    "anthropic": ("made-up-model", history),
    "chatgpt": ("made-up-model", history),
    "claude-code": ("made-up-model", history),
    "decisions": ("model-routing", routing),
    "ollama": ("qwen3:4b", history),
}


def sent_by(monkeypatch, tmp_path, provider, *, redact, decisions="https://decisions.example.test"):
    """Start Pi with every provider configured, ask one of them, and return what left Pi."""
    for name in list(os.environ):
        if name.startswith("PI_"):
            monkeypatch.delenv(name)
    for name, value in {
        "PI_ADMIN_KEY": "synthetic-admin-key-only",
        "PI_DB_PATH": str(tmp_path / "startup.db"),
        "PI_OPENROUTER_KEY": "synthetic",
        "PI_OPENAI_KEY": "synthetic",
        "PI_ANTHROPIC_KEY": "synthetic",
        "PI_ALLOW_PAID_MODELS": "1",
        "PI_SUBSCRIPTION_URL": "http://127.0.0.1:9/",
        "PI_SUBSCRIPTION_KEY": "s" * 32,
        "PI_DECISION_URL": decisions,
        "PI_DECISION_KEY": "d" * 32,
    }.items():
        monkeypatch.setenv(name, value)
    bodies = []

    def handle(transport, request):
        request.read()
        if request.url.path.endswith("/api/v1/models"):
            return httpx.Response(200, json={"data": [FREE]}, request=request)
        bodies.append(request.content.decode())
        return httpx.Response(503, json={"error": "synthetic"}, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)

    async def run():
        async with startup.lifespan(api.app):
            if redact:
                choose(api.app.state.store, {"privacy.redact": True})
            adapter = api.app.state.router.adapters()[provider]
            model, messages = ASKED[provider]
            with pytest.raises(ProviderUnavailable):
                adapter.complete_bounded(messages(), model=model, timeout=5)

    asyncio.run(run())
    assert bodies, f"{provider} sent nothing"
    return "\n".join(bodies)


@pytest.mark.parametrize("provider", [name for name in ASKED if name != "ollama"])
def test_every_online_model_is_sent_only_the_redacted_copy(monkeypatch, tmp_path, provider):
    sent = sent_by(monkeypatch, tmp_path, provider, redact=True)
    leaked = [secret[:12] for secret in SECRETS if secret in sent]
    assert leaked == []
    assert "[removed: key]" in sent and "[removed: card number]" in sent


def test_online_models_get_the_request_as_it_is_while_the_setting_is_off(monkeypatch, tmp_path):
    sent = sent_by(monkeypatch, tmp_path, "openai", redact=False)
    assert all(secret in sent for secret in SECRETS) and "[removed:" not in sent


@pytest.mark.parametrize("provider", ["ollama", "decisions"])
def test_local_models_get_the_request_as_it_is(monkeypatch, tmp_path, provider):
    sent = sent_by(monkeypatch, tmp_path, provider, redact=True, decisions="http://127.0.0.1:8060")
    assert OPENAI_SHAPE in sent and CARD in sent and "[removed:" not in sent


class Echo(FakeGate):
    def invoke(self, tool_id, args, approval_request_id=None, **kwargs):
        self.invocations.append((tool_id, args, approval_request_id))
        return ToolResult(ok=True, result={"echoed": args["text"], "pem": pem()}, tool_id=tool_id)


def test_a_turn_hides_secrets_but_keeps_the_chat_and_the_tool_arguments(tmp_path):
    from pi.models import redaction

    call = json.dumps({"tool": "t_echo", "args": {"text": GITHUB_SHAPE}})
    with closing(Store(tmp_path / "pi.db")) as store:
        choose(store, {"privacy.redact": True})
        online = Scripted([call, "done"])
        gate = Echo()
        router = Router(local_provider=redaction.Redacting(online, store), local_model="m")
        session = store.create_session()
        turn = Loop(store, router, toolgate=gate).run_turn(session, f"Run it. Card {CARD}")
        # The tool request the model returned runs exactly as it was written.
        assert gate.invocations == [("t_echo", {"text": GITHUB_SHAPE}, None)]
        # The chat keeps every word; only the model's copy changed.
        stored = json.dumps(store.messages(session))
        assert GITHUB_SHAPE in stored and CARD in stored and "[removed:" not in stored
        second = online.sent[-1]
        text = "\n".join(m.content for m in second)
        assert GITHUB_SHAPE not in text and CARD not in text and "FAKE" * 16 not in text
        for message in second:
            if message.role in ("assistant", "tool"):
                json.loads(message.content)  # the tool request and result stay JSON
        receipt = research_usage.receipt(store, turn["turn_id"])
        assert [row["redacted"] for row in receipt["attempts"]] == [
            {"card": 1},
            {"card": 1, "key": 2, "private_key": 1},
        ]
        # Counts only, and a count that is not one shows as none.
        for stored in ('{"key": -1}', '{"made_up": 1}', "not json", '["key"]', "{}"):
            with closing(sqlite3.connect(store.path)) as db:
                db.execute("UPDATE research_model_calls SET redacted=?", (stored,))
                db.commit()
            receipt = research_usage.receipt(store, turn["turn_id"])
            assert [row["redacted"] for row in receipt["attempts"]] == [None, None]
