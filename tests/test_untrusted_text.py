"""Hidden code points never reach a model, whatever the text came from (attack report fix 3),
and tool results reach every provider labelled as outside content (fix 4).

Every model input is a Message, so the cleaner sits there; these tests drive each entry point
through its real path and look at what the provider was sent.
"""

import hashlib
import json
import unicodedata
from contextlib import closing

import httpx
import pytest
from test_loop import Recorder, loop_with
from test_memory_prompt import empty_package
from test_project_context import setup
from test_tool_turns import CALL, FakeGate, build

from pi.actions.toolgate import ToolResult
from pi.content import attachments
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.core.untrusted_text import strip_invisible
from pi.memory import store as memory_store
from pi.models import providers
from pi.models.claude_code import ClaudeCodeProvider
from pi.models.evidence import INSTRUCTIONS
from pi.models.providers import Message
from pi.models.routing import Router
from pi.schedule import agent_hooks, agent_jobs
from pi.schedule.job_models import AgentJobCreate, Timing
from pi.turns.loop import Loop

# A zero-width space, a Unicode tag message, an emoji variation selector, a bidi override and a
# Hangul filler: a reader sees "payment", a model would read the rest.
ZWSP, TAG_A, VS16, RLO, FILLER = chr(0x200B), chr(0xE0041), chr(0xFE0F), chr(0x202E), chr(0x3164)
HIDDEN = f"pay{ZWSP}{TAG_A}{VS16}{RLO}{FILLER}ment"
SECRETS = {ZWSP, TAG_A, VS16, RLO, FILLER}

# ToolGate (toolgate/core/untrusted_text.py) pins the same value: both gates remove the same set.
KEPT_FINGERPRINT = ("15.0.0", "1faf4104604facb3a3be2781d099a321a65232c0855ae8bd9d3eb69bb4fa9e3a")


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def clean(sent):
    """Every message the provider got is free of the hidden code points, and the text is there."""
    text = "\n".join(m.content for m in sent)
    assert not SECRETS & set(text)
    assert "payment" in text


def test_removed_set_matches_toolgate():
    kept = strip_invisible("".join(map(chr, range(0x110000))))
    digest = hashlib.sha256(kept.encode("utf-8", "surrogatepass")).hexdigest()
    assert (unicodedata.unidata_version, digest) == KEPT_FINGERPRINT


def test_a_message_keeps_layout_and_loses_hidden_code_points():
    assert Message("user", HIDDEN + "\nline\ttab").content == "payment\nline\ttab"


def test_project_context(store):
    _, target, _, _ = setup(store, text=HIDDEN)
    provider = Recorder()
    loop_with(store, provider).run_turn(target, "Use evidence", request_id="hidden-project-turn-1")
    clean(provider.calls[0])


def test_memory_recall_and_so_chat_imports(store):
    # Imported chats reach a model only as recalled memory.
    package = empty_package("selected")
    package["memories"] = [{"id": "m1", "text": HIDDEN, "confidence": "low"}]
    session = store.create_session()
    turn = store.start_turn(session)
    store.append_message(session, "user", "What do I owe?", turn_id=turn, purpose="input")
    memory_store.save_context(store, turn, "ok", package)
    clean(Loop(store, Router())._history(session, turn_id=turn))


def test_an_attached_file(store):
    loop, provider = build(store, ["Read it."], FakeGate())
    session = store.create_session()
    identity = attachments.upload(
        store,
        session,
        attachments.Metadata(name="note.txt", type="text/plain"),
        HIDDEN.encode(),
        resolve=session_settings.source_privacy,
    )["id"]
    loop.run_turn(session, "Read this", request_id="hidden-file-turn-1", attachment_ids=[identity])
    clean(provider.sent[0])


def test_a_webhook_payload(store):
    job = agent_jobs.create(
        store,
        AgentJobCreate(
            name="Mail triage",
            instructions="Sort what arrives.",
            timing=Timing(kind="interval", time="00:00", day=0, hours=24),
            timeZone="Asia/Jerusalem",
            clock=False,
        ),
    )
    token = agent_hooks.issue(store, job["id"])["token"]
    run = agent_hooks.receive(store, job["id"], token, {"text": HIDDEN})
    loop, provider = build(store, ["NOTHING"], FakeGate())
    assert agent_jobs.AgentJobs(store, loop).run(run["runId"])["status"] == "completed"
    clean(provider.sent[0])


class HiddenGate(FakeGate):
    """A tool (an MCP server's, say) whose answer carries hidden text."""

    def invoke(self, tool_id, args, approval_request_id=None, *, action_id=None, job_id=None):
        self.invocations.append((tool_id, args, approval_request_id))
        return ToolResult(ok=True, result={"text": HIDDEN}, tool_id=tool_id)


def test_a_tool_result_and_the_transcript_keeps_what_arrived(store):
    loop, provider = build(store, [CALL, "done"], HiddenGate())
    session = store.create_session()
    loop.run_turn(session, "use the tool")
    clean(provider.sent[1])
    # Only the model's copy is cleaned; the record keeps the tool's exact answer.
    (stored,) = [m for m in store.messages(session) if m["role"] == "tool"]
    assert HIDDEN in stored["content"]


TOOL_TURN = [
    Message("user", "Check the invoice"),
    Message("assistant", CALL),
    Message("tool", json.dumps({"tool": "t_echo", "result": {"text": HIDDEN}})),
]


def labelled(rows):
    """Roles and contents as a provider received them: the tool result is outside content."""
    assert [row["role"] for row in rows].count("tool") == 0
    assert sum(row["content"] == INSTRUCTIONS for row in rows) == 1
    (evidence,) = [json.loads(r["content"]) for r in rows if r["content"].startswith('{"type"')]
    assert evidence["type"] == "conker_tool_result" and evidence["source_role"] == "tool"


def test_ollama_labels_tool_results_as_outside_content(monkeypatch):
    sent = []

    def post(url, **kwargs):
        sent.append(kwargs["json"])
        return httpx.Response(
            200, json={"message": {"content": "ok"}}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", post)
    adapter = providers.OllamaProvider("http://127.0.0.1:1")
    adapter.complete_bounded(TOOL_TURN, model="qwen3:4b", timeout=5)
    labelled(sent[0]["messages"])
    # A turn with no tool result is sent as it is.
    adapter.complete_bounded(TOOL_TURN[:1], model="qwen3:4b", timeout=5)
    assert sent[1]["messages"] == [{"role": "user", "content": "Check the invoice"}]


def test_claude_code_labels_tool_results_as_outside_content():
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"text": "ok", "model": "test"})

    provider = ClaudeCodeProvider("", transport=httpx.MockTransport(handle))
    provider.complete_bounded(TOOL_TURN, model="test", timeout=5)
    labelled(sent[0]["messages"])
