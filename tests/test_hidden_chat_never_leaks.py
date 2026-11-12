"""Property: nothing from a hidden chat reaches any list, search or other chat's model input.

One chat carries a unique marker in every place Pi keeps its words: title, messages, an artifact,
a project-source link, MemoryGate records cited or derived from it, a memory Conker kept from it,
a task, an agent and an AI job it made, and an open job proposal. A visible control chat carries
its own marker the same way. For each hidden state (in the Trash; forgotten) every read path the
visibility groups wired is called, and the hidden marker must appear in none of the outputs while
the control marker still does. A trashed chat that is restored shows its marker again.
"""

import json
import time
import types
from contextlib import closing, suppress
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_loop import Recorder as LoopRecorder
from test_loop import loop_with
from test_visibility_memory import explorer
from test_visibility_search import Library

from pi.agents import keep
from pi.agents import profiles as agents
from pi.content import artifact_models, artifacts
from pi.context import controls as context_controls
from pi.context import project_sources, projects
from pi.context import universal_search as search
from pi.conversation import archive, forgetting, submissions
from pi.conversation import history as session_history
from pi.conversation import search as conversation_search
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.memory.hidden import _receipt_ids
from pi.memory.service import without_hidden
from pi.models.providers import Completion
from pi.schedule import agent_jobs, job_models, proposals, tasks

AGENT = "companion"
COMMON = "zqmark"
HIDDEN = "zqmark-hidden-7731"
CONTROL = "zqmark-control-4482"
DAY = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)  # 15:00 in the default zone: not quiet hours
NEXT_DAY = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)  # a new day, so a new suggestion budget
LEXICAL = ("conversations", "artifacts", "tasks", "agents", "memory")
REFUSED = (agents.AgentError, context_controls.ContextError)


def clock_at(monkeypatch, module, at):
    """`module` reads `at` as the time now; the rest of the time module stays real."""
    names = {name: getattr(time, name) for name in dir(time) if not name.startswith("_")}
    monkeypatch.setattr(module, "time", types.SimpleNamespace(**{**names, "time": lambda: at}))


def say(store, session, texts, at, role="user"):
    """Messages written at a chosen time (proposal passes read by time window)."""
    clock, ids = [at], []
    real = submissions.time
    submissions.time = types.SimpleNamespace(time=lambda: clock[0])
    try:
        for text in texts:
            ids.append(store.append_message(session, role, text)["id"])
            clock[0] += 1
    finally:
        submissions.time = real
    return ids


class Model:
    """The proposals model: records each prompt and proposes one idea citing e1-e3."""

    def __init__(self, title):
        self.title, self.prompts = title, []

    def __call__(self, messages):
        self.prompts.append("\n".join(message.content for message in messages))
        item = {
            "title": self.title,
            "noticed": "You asked for this three times.",
            "suggestion": "Prepare it for you.",
            "ifApproved": "A draft appears in your Inbox; nothing is sent.",
            "evidence": ["e1", "e2", "e3"],
        }
        return Completion(text=json.dumps({"proposals": [item]}), model="m", provider="local")


def job_name(mark):
    """An AI job's name (its own chat takes it as a title): `mark` with "job" inside, so the
    job's chat is checked on its own (test_the_chat_of_a_job_kept_from_a_hidden_chat_...)."""
    base, number = mark.rsplit("-", 1)
    return f"{base}-job-{number}"


def seed_chat(store, mark, at):
    """A chat whose every kept trace carries `mark`; returns the ids the fakes need."""
    sid = store.create_session(title=f"{mark} title")
    said = say(store, sid, [f"{mark} report {n}" for n in range(3)], at)
    answer = store.complete_turn(store.start_turn(sid), f"{mark} answer")["id"]
    artifacts.create(
        store,
        artifact_models.FromMessage(title=f"{mark} artifact", sessionId=sid, messageId=answer),
        resolve=session_settings.source_privacy,
    )
    tasks.create(
        store,
        tasks.CreateTask(
            request_id=f"leak_task_{sid}", session_id=sid, outcome=f"{mark} outcome", criteria=["x"]
        ),
    )
    agent = agents.create(
        store,
        agents.AgentInput(
            name=f"{mark} agent",
            role=f"{mark} role",
            instructions=f"{mark} instructions",
            modelId=None,
            toolIds=[],
            memory=agents.MemorySelection(scope="none", memoryIds=[]),
        ),
    )["id"]
    job = agent_jobs.create(
        store,
        job_models.AgentJobCreate.model_validate(
            dict(
                name=job_name(mark),
                instructions="Look around.",
                timing=dict(kind="interval", time="00:00", day=0, hours=2),
                timeZone="Asia/Jerusalem",
            )
        ),
        now=DAY.timestamp(),
        created_by="conker",  # Conker's own job: one he adopted or approved stays (jobs.ADOPTED)
    )["id"]
    delivered = store.append_message(sid, "user", f"{mark} delivered")["id"]
    kept_memory = f"mem-kept-{sid}"
    with store._connect() as db:
        db.execute(
            "UPDATE memory_outbox SET destination_agent_id=? WHERE message_id=?", (AGENT, delivered)
        )
        keep.record(
            db,
            {"kind": "memory", "id": f"req_mem_{sid}", "sessionId": sid},
            {"status": "remembered", "memoryId": kept_memory},
        )
        for kind, target in (("agent", agent), ("ai_job", job)):
            db.execute(
                "INSERT INTO keep_results(request_id,session_id,kind,target_id,created_at) "
                "VALUES (?,?,?,?,0)",
                (f"req_{kind}_{sid}", sid, kind, target),
            )
    evidence, analysis, memory = _receipt_ids(AGENT, delivered)
    return {
        "sid": sid,
        "said": said,
        "mark": mark,
        "records": {"evidence": evidence, "analysis": analysis, "memory": memory},
        "kept_memory": kept_memory,
        "job": job,
    }


def link_project(store, chats):
    """One project with both chats as sources, and one target chat reading each source."""
    project = projects.create(
        store, projects.Fields(name="Research", description="", instructions="")
    )
    targets = {}
    for revision, chat in enumerate(chats, start=1):
        reference = {"kind": "conversation", "sessionId": chat["sid"]}
        projects.mutate(
            store,
            project["id"],
            projects.Link(expected_revision=revision, reference=reference),
            "link",
            lambda ref: project_sources.resolve(store, ref),
        )
        target = store.create_session(title="reader")
        settings = session_settings.Settings(
            agentId="companion",
            projectId=project["id"],
            projectSources=[reference],
            privacy=session_settings.Privacy(memoryDisabled=False, harnessDisabled=False),
        )
        session_settings.save(
            store, target, session_settings.Update(expected_revision=0, settings=settings)
        )
        targets[chat["mark"]] = target
    return project["id"], targets


def recall_package(chats):
    """What MemoryGate could answer: every record kind that points back at a chat."""
    memories, evidence = [], []
    for chat in chats:
        mark, sid = chat["mark"], chat["sid"]
        memories += [
            {"id": chat["records"]["memory"], "text": f"{mark} derived"},
            {"id": chat["kept_memory"], "text": f"{mark} kept"},
            {"id": f"cite-session-{sid}", "text": mark, "citations": [{"session_id": sid}]},
            {
                "id": f"cite-message-{sid}",
                "text": mark,
                "citations": [{"message_id": chat["said"][0]}],
            },
        ]
        evidence.append({"id": chat["records"]["evidence"], "text": f"{mark} evidence"})
    return {"memories": memories, "evidence": evidence, "retrieval": {}}


def cards(chats):
    return [
        {"type": kind, "id": identity, "title": f"{chat['mark']} {kind}", "preview": chat["mark"]}
        for chat in chats
        for kind, identity in [*chat["records"].items(), ("memory", chat["kept_memory"])]
    ]


def build(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        hidden = seed_chat(store, HIDDEN, DAY.timestamp() - 600)
        control = seed_chat(store, CONTROL, NEXT_DAY.timestamp() - 600)
        project, targets = link_project(store, [hidden, control])
        # Pass 1 reads only the hidden chat's words (the control's are in the next window).
        assert proposals.run_pass(store, Model(f"{HIDDEN} plan"), now=DAY)["created_count"] == 1
    return path, hidden, control, project, targets


def hide(path, state, hidden, monkeypatch):
    """Put the chat in the Trash, or forget it, just after pass 1 (both before pass 2)."""
    with monkeypatch.context() as patch:
        if state == "trashed":
            clock_at(patch, archive, DAY.timestamp() + 60)
            with closing(Store(path)) as store:
                archive.trash(store, archive.Trash(sessions=[hidden["sid"]], trashed=True))
                # The owner writes in it opened by its link; no other chat may read this.
                say(
                    store,
                    hidden["sid"],
                    [f"{HIDDEN} written while trashed"],
                    NEXT_DAY.timestamp() - 800,
                )
        else:
            clock_at(patch, forgetting, DAY.timestamp() + 60)
            sid = hidden["sid"]
            forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])


def project_turn(store, target, request_id):
    provider = LoopRecorder()
    with suppress(*REFUSED):  # a hidden source refuses the turn before any model call
        loop_with(store, provider).run_turn(target, "Use it", request_id=request_id)
    return [[m.content for m in call] for call in provider.calls]


def read_all(store, chats, project, targets, round_id):
    """Every read path the visibility groups wired, as text, keyed by path."""
    memory = SimpleNamespace(client=Library(cards(chats)))
    package = recall_package(chats)
    resolve = lambda ref: project_sources.resolve(store, ref)  # noqa: E731
    out = {
        "chat search": [conversation_search.search(store, q) for q in (COMMON, HIDDEN)],
        "chat list": session_history.page(store),
        "semantic memory": search.visible_memories(store, package["memories"], (AGENT,)),
        "recall": without_hidden(store, package, (AGENT,)),
        "project": projects.get(store, project, resolve),
        "project search": projects.search(store, project, COMMON, resolve),
        "proposal list": proposals.list_proposals(store, "all"),
    }
    for source in LEXICAL:
        for stage in ("metadata", "text"):
            out[f"universal {source} {stage}"] = [
                search.lexical(store, memory, None, q, {"sources": [source]}, stage)[0]
                for q in (COMMON, HIDDEN)
            ]
    client, _, gate = explorer(
        store, lambda body: {"scope": "all", "objects": cards(chats), "next_after": None}
    )
    try:
        out["explorer"] = client.get("/memory/objects").json()
        hidden_memory = chats[0]["records"]["memory"]
        detail = client.get(f"/memory/objects/memory/{hidden_memory}")
        out["explorer detail"] = detail.json() if detail.status_code == 200 else None
    finally:
        gate.close()
    for mark, target in targets.items():
        out[f"project turn {mark}"] = project_turn(store, target, f"leak_{round_id}_{mark[7:13]}")
    return {name: json.dumps(value, default=str) for name, value in out.items()}


def listed_jobs(store):
    """Ids the universal search's jobs source shows (each job's name matches "job")."""
    rows, _ = search.lexical(store, None, None, "job", {"sources": ["jobs"]}, "metadata")
    return {row["recordId"] for row in rows}


def activity_chats(store):
    rows, _ = search.lexical(store, None, None, "created", {"sources": ["activity"]}, "metadata")
    with store._connect() as db:
        events = dict(db.execute("SELECT id,session_id FROM activity_events").fetchall())
    return {events[row["recordId"]] for row in rows}


# The control chat's marker must still show here (the explorer detail asks for a hidden id).
SHOWS = {
    "chat search",
    "chat list",
    "semantic memory",
    "recall",
    "project",
    "project search",
    "proposal list",
    "explorer",
    f"project turn {CONTROL}",
    *(f"universal {source} metadata" for source in LEXICAL),
}


@pytest.mark.parametrize("state", ["trashed", "forgotten"])
def test_a_hidden_chat_leaks_into_no_list_search_or_model_input(tmp_path, monkeypatch, state):
    path, hidden, control, project, targets = build(tmp_path)
    hide(path, state, hidden, monkeypatch)
    chats = [hidden, control]
    with closing(Store(path)) as store:
        say(store, control["sid"], [f"{CONTROL} again"], NEXT_DAY.timestamp() - 300)
        model = Model(f"{CONTROL} plan")
        assert proposals.run_pass(store, model, now=NEXT_DAY)["created_count"] == 1
        outputs = read_all(store, chats, project, targets, "hidden")
        outputs["proposal model input"] = model.prompts[0]
        leaked = sorted(name for name, text in outputs.items() if HIDDEN in text)
        assert leaked == []
        missing = sorted(
            name for name in SHOWS | {"proposal model input"} if CONTROL not in outputs[name]
        )
        assert missing == []
        assert outputs["explorer detail"] == "null"  # the hidden record is refused by id
        assert hidden["sid"] not in activity_chats(store)
        assert control["sid"] in activity_chats(store)
        assert hidden["job"] not in listed_jobs(store) and control["job"] in listed_jobs(store)
        if state == "forgotten":
            return
        # The exceptions: the Trash view and the chat opened by its own link still show it.
        trash_view = session_history.page(store, trashed="only")
        assert [row["id"] for row in trash_view["results"]] == [hidden["sid"]]
        assert any(HIDDEN in m["content"] for m in store.messages(hidden["sid"]))
        archive.trash(store, archive.Trash(sessions=[hidden["sid"]], trashed=False))
        restored = read_all(store, chats, project, targets, "restored")
        back = {"explorer detail", f"project turn {HIDDEN}"} | SHOWS - {f"project turn {CONTROL}"}
        absent = sorted(name for name in back if HIDDEN not in restored[name])
        assert absent == []
        assert hidden["sid"] in activity_chats(store)
        assert hidden["job"] in listed_jobs(store)


def job_chat_shows(path, hidden, state, monkeypatch, where):
    """Where the name of an AI job kept from the hidden chat still shows, of `where`."""
    hide(path, state, hidden, monkeypatch)
    name = job_name(HIDDEN)
    with closing(Store(path)) as store:
        shown = {
            "chat list": lambda: session_history.page(store),
            "chat titles": lambda: search.conversation_metadata(store, name)[0],
        }
        return [place for place in where if name in json.dumps(shown[place]())]


@pytest.mark.parametrize("state", ["trashed", "forgotten"])
def test_the_chat_of_a_job_kept_from_a_hidden_chat_does_not_show_in_title_search(
    tmp_path, monkeypatch, state
):
    path, hidden, _, _, _ = build(tmp_path)
    assert job_chat_shows(path, hidden, state, monkeypatch, ["chat titles"]) == []


@pytest.mark.parametrize("state", ["trashed", "forgotten"])
def test_the_chat_of_a_job_kept_from_a_hidden_chat_does_not_show_in_the_chat_list(
    tmp_path, monkeypatch, state
):
    path, hidden, _, _, _ = build(tmp_path)
    assert job_chat_shows(path, hidden, state, monkeypatch, ["chat list"]) == []
