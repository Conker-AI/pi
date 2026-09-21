"""Real Loop call dispatch with fake providers; all databases/media are synthetic."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from pi import agents, context_controls, forgetting, session_settings
from pi import calls as c
from pi.loop import Loop
from pi.providers import Completion, ProviderUnavailable
from pi.routing import Router
from pi.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "calls.db")
    yield value
    value.close()


class Provider:
    name = "one"

    def __init__(self, callback=None):
        self.seen, self.callback = [], callback

    def complete(self, messages, *, model):
        self.seen.append((messages, model))
        if self.callback:
            self.callback()
        return Completion(text="actual provider answer", model=model, provider=self.name)

    def complete_bounded(self, messages, *, model, timeout):
        return self.complete(messages, model=model)


class Speech:
    def __init__(self, callback=None, fail_tts=False):
        self.stt, self.tts, self.callback, self.fail_tts = 0, 0, callback, fail_tts

    def capabilities(self):
        return {"stt": {"status": "configured"}, "tts": {"status": "configured"}}

    def transcribe(self, raw, mime):
        self.stt += 1
        if self.callback:
            self.callback()
        return {"text": "spoken question", "segments": None, "duration_seconds": 1}

    def synthesize(self, text):
        self.tts += 1
        if self.fail_tts:
            raise RuntimeError("synthetic transport failure")
        return {"audio": b"transient audio", "mime": "audio/wav", "duration_seconds": 1}


def start(store, source=None):
    source = source or store.create_session()
    return c.start(store, c.Start(request_id="start_request_857300", conversationId=source))


def runtime(store, provider=None, gate=None):
    provider = provider or Provider()
    return Loop(store, Router(local_provider=provider, local_model="fake"), toolgate=gate), provider


def send(store, loop, call, number=1, **kwargs):
    return c.run(
        store,
        loop,
        call["id"],
        c.Send(request_id=f"call_request_8573_{number}", text="typed question"),
        **kwargs,
    )


def test_real_typed_turn_replay_history_and_independent_conversation(store):
    source = store.create_session()
    original = store.append_message(source, "user", "earlier exact context")
    call = start(store, source)
    assert store.get_session(call["sessionId"])["parent_id"] == source
    loop, provider = runtime(store)
    result = send(store, loop, call)
    assert result["call"]["requests"][0]["state"] == "complete"
    assert result["call"]["requests"][0]["speechStatus"] == "unavailable"
    assert result["call"]["requests"][0]["textStatus"] == "complete"
    assert any(m.content == "earlier exact context" for m in provider.seen[0][0])
    assert store.messages(source) == [original]
    assert [m["role"] for m in store.messages(call["sessionId"])] == ["user", "assistant"]
    assert all("source" in e for e in result["call"]["events"] if e["kind"] != "event")
    assert send(store, loop, call)["replayed"] is True and len(provider.seen) == 1
    path, identity = store.path, call["id"]
    store.close()
    reopened = Store(path)
    try:
        assert c.get(reopened, identity)["requests"] == result["call"]["requests"]
    finally:
        reopened.close()


def test_pause_before_send_and_microphone_keyboard_independence(store):
    call = start(store)
    loop, provider = runtime(store)
    call = c.update(store, call["id"], c.Update(expected_revision=1, paused=True))
    with pytest.raises(c.CallError, match="paused"):
        send(store, loop, call)
    assert not provider.seen
    call = c.update(
        store,
        call["id"],
        c.Update(expected_revision=2, paused=False, channels=c.ChannelUpdate(voice=False)),
    )
    assert send(store, loop, call)["call"]["requests"][0]["state"] == "complete"
    assert call["channels"]["microphone"] is False
    with pytest.raises(c.CallError, match="input channel"):
        c.run(
            store,
            loop,
            call["id"],
            c.Send(request_id="audio_request_8573"),
            audio=b"audio",
            speech=Speech(),
        )
    with pytest.raises(c.CallError, match="Camera"):
        c.update(
            store, call["id"], c.Update(expected_revision=3, channels=c.ChannelUpdate(camera=True))
        )


def test_pause_during_actual_provider_blocks_answer_tools_and_speech(store):
    from test_tool_turns import FakeGate

    entered, release = Event(), Event()
    call = start(store)
    provider = Provider(lambda: (entered.set(), release.wait(5)))
    speech, gate = Speech(), FakeGate()
    loop, _ = runtime(store, provider, gate)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(send, store, loop, call, speech=speech)
        assert entered.wait(5)
        c.update(store, call["id"], c.Update(expected_revision=1, paused=True))
        release.set()
        result = future.result(timeout=5)
    assert result["audio"] is None and speech.tts == 0 and not gate.invocations
    assert [m["role"] for m in store.messages(call["sessionId"])] == ["user"]
    assert result["call"]["requests"][0]["state"] == "failed"
    assert result["call"]["requests"][0]["turnStatus"] == "failed"


def test_already_inflight_tool_outcome_remains_truthful(store):
    from test_tool_turns import CALL, FakeGate, Scripted

    call = start(store)

    class Gate(FakeGate):
        def invoke(self, *args, **kwargs):
            result = super().invoke(*args, **kwargs)
            c.interrupt(store, call["id"], c.Revision(expected_revision=1), ended=True)
            return result

    gate, provider = Gate(), Scripted([CALL, "must not produce a next answer"])
    loop, _ = runtime(store, provider, gate)
    result = send(store, loop, call)
    assert len(gate.invocations) == 1
    assert len(provider.sent) == 1
    receipt = result["call"]["requests"][0]
    assert receipt["acted"] is True and receipt["turnStatus"] == "acted_no_reply"
    assert result["call"]["phase"] == "ended"


def test_stt_loop_tts_is_once_and_output_is_transient(store):
    call = start(store)
    call = c.update(
        store,
        call["id"],
        c.Update(expected_revision=1, channels=c.ChannelUpdate(microphone=True, keyboard=False)),
    )
    loop, provider = runtime(store)
    speech = Speech()
    body = c.Send(request_id="spoken_request_8573")
    result = c.run(store, loop, call["id"], body, audio=b"synthetic pcm", speech=speech)
    assert result["audio"]["audio"] == b"transient audio"
    assert (speech.stt, speech.tts, len(provider.seen)) == (1, 1, 1)
    assert store.messages(call["sessionId"])[0]["content"] == "spoken question"
    assert (
        c.run(store, loop, call["id"], body, audio=b"synthetic pcm", speech=speech)["audio"] is None
    )
    assert (speech.stt, speech.tts, len(provider.seen)) == (1, 1, 1)
    with pytest.raises(c.CallError, match="different input"):
        c.run(store, loop, call["id"], body, audio=b"changed", speech=speech)
    assert b"synthetic pcm" not in store.path.read_bytes()
    assert b"transient audio" not in store.path.read_bytes()


def test_tts_failure_retains_text_success_and_no_retry(store):
    call, speech = start(store), Speech(fail_tts=True)
    loop, provider = runtime(store)
    result = send(store, loop, call, speech=speech)
    request = result["call"]["requests"][0]
    assert request["state"] == "complete" and request["textStatus"] == "complete"
    assert request["speechStatus"] == "failed-or-interrupted"
    assert result["audio"] is None
    send(store, loop, call, speech=speech)
    assert speech.tts == 1 and len(provider.seen) == 1


def test_source_privacy_and_policy_pins_are_inherited_separately(store):
    source = store.create_session()
    included = store.append_message(source, "user", "exact pinned words")
    excluded = store.append_message(source, "user", "excluded words")
    session_settings.save(
        store,
        source,
        session_settings.Update(
            expected_revision=0,
            settings=session_settings.Settings(
                agentId="companion",
                privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=True),
            ),
        ),
    )
    policy = context_controls.Policy(
        sessionInstructions="Inherited instruction",
        messagePolicies={included["id"]: "keep-exact", excluded["id"]: "exclude"},
        budget=context_controls.Budget(
            contextWindowTokens=20000, outputReserveTokens=1000, otherInputTokens=0
        ),
    )
    context_controls.save(
        store, source, context_controls.Update(expected_revision=0, policy=policy)
    )
    call = start(store, source)
    assert call["privacy"] == {"memory": True, "harness": True}
    loop, provider = runtime(store)
    send(store, loop, call)
    prompt = [m.content for m in provider.seen[0][0]]
    assert "exact pinned words" in prompt and "excluded words" not in prompt
    assert "Inherited instruction" in prompt
    with pytest.raises(c.CallError, match="cannot be relaxed"):
        c.update(store, call["id"], c.Update(expected_revision=1, privacy=c.Privacy()))
    with pytest.raises(agents.AgentError):
        session_settings.save(
            store,
            call["sessionId"],
            session_settings.Update(
                expected_revision=1,
                settings=session_settings.Settings(
                    agentId="companion",
                    privacy=session_settings.Privacy(memoryDisabled=False, harnessDisabled=False),
                ),
            ),
        )
    assert session_settings.load(store, source)["settings"]["privacy"]["memoryDisabled"] is True


def test_summary_is_referenced_and_source_changes_stop_next_turn(store):
    source = store.create_session(summary="Earlier untrusted summary")
    call = start(store, source)
    assert store.get_session(call["sessionId"])["summary"] is None
    loop, provider = runtime(store)
    send(store, loop, call)
    assert any("Earlier untrusted summary" in m.content for m in provider.seen[0][0])
    with store._connect() as db:
        db.execute("UPDATE sessions SET summary='changed' WHERE id=?", (source,))
    with pytest.raises(c.CallError, match="summary changed"):
        send(store, loop, call, 2)
    assert len(provider.seen) == 1


def test_recovery_marks_unknown_and_never_restarts_work(store):
    call = start(store)
    with store._connect() as db:
        db.execute(
            "INSERT INTO call_requests(request_id,call_id,generation,payload_hash,input_kind,"
            "state,created_at,updated_at) "
            "VALUES('unknown_request_8573',?,1,'digest','audio','transcribing',1,1)",
            (call["id"],),
        )
    path = store.path
    store.close()
    reopened = Store(path)
    try:
        assert c.recover(reopened) == 1
        view = c.get(reopened, call["id"])
        assert view["paused"] is True and view["requests"][0]["state"] == "unknown"
        assert view["generation"] == 2
    finally:
        reopened.close()


def test_parent_forgetting_erases_call_transcript_and_hashes(store):
    source = store.create_session(summary="call-secret-summary-8573")
    call = start(store, source)
    loop, _ = runtime(store)
    body = c.Send(request_id="private_request_8573", text="call-secret-message-8573")
    c.run(store, loop, call["id"], body)
    path = store.path
    store.close()
    receipt = forgetting.forget(path, source, forgetting.preview(path, source)["confirmation"])
    assert set(receipt["session_ids"]) == {source, call["sessionId"]}
    reopened = Store(path)
    try:
        with pytest.raises(c.CallError):
            c.get(reopened, call["id"])
        with reopened._connect() as db:
            assert db.execute("SELECT payload_hash,preferences FROM call_requests").fetchone()[
                :
            ] == (None, None)
    finally:
        reopened.close()
    for file in path.parent.glob("calls.db*"):
        assert b"call-secret-summary-8573" not in file.read_bytes()
        assert b"call-secret-message-8573" not in file.read_bytes()


def test_model_preference_changes_only_future_accepted_requests(store):
    from test_model_roles import config

    from pi import model_roles

    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(config())
        ),
    )
    call = start(store)
    call = c.update(
        store,
        call["id"],
        c.Update(
            expected_revision=1, modelId="a", channels=c.ChannelUpdate(microphone=True, voice=False)
        ),
    )
    first, second = Provider(), Provider()
    second.name = "two"
    loop = Loop(store, Router(local_provider=first, local_model="a", hosted_provider=second))
    speech = Speech(
        callback=lambda: c.update(store, call["id"], c.Update(expected_revision=2, modelId="b"))
    )
    result = c.run(
        store,
        loop,
        call["id"],
        c.Send(request_id="model_freeze_857300"),
        speech=speech,
        audio=b"synthetic pcm",
    )
    assert result["call"]["requests"][0]["state"] == "complete"
    assert first.seen[0][1] == "actual-a" and not second.seen
    send(store, loop, call, 2)
    assert second.seen[0][1] == "actual-b"
    assert session_settings.load(store, call["conversationId"])["settings"].get("modelId") is None


def test_interruption_blocks_configured_model_fallback(store):
    from test_model_roles import config

    from pi import model_roles

    configuration = config()
    configuration["roleSettings"]["answerMode"] = "router"
    assignment = dict(
        configuration["roleSettings"]["roles"]["answer"], failure="fallback", fallbackModelId="b"
    )
    configuration["roleSettings"]["roles"]["routing"] = assignment
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0,
            configuration=model_roles.Configuration.model_validate(configuration),
        ),
    )
    call = start(store)

    def stop_then_fail():
        c.interrupt(store, call["id"], c.Revision(expected_revision=1))
        raise ProviderUnavailable("synthetic provider outage")

    first, second = Provider(stop_then_fail), Provider()
    second.name = "two"
    loop = Loop(store, Router(local_provider=first, local_model="a", hosted_provider=second))
    result = send(store, loop, call)
    assert result["call"]["requests"][0]["state"] == "failed"
    assert len(first.seen) == 1 and not second.seen


def test_atomic_final_commit_rechecks_generation(store, monkeypatch):
    call = start(store)
    loop, _ = runtime(store)
    original = store.complete_turn

    def stop_at_commit(*args, **kwargs):
        c.interrupt(store, call["id"], c.Revision(expected_revision=1), ended=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "complete_turn", stop_at_commit)
    result = send(store, loop, call)
    assert result["call"]["requests"][0]["turnStatus"] == "failed"
    assert [m["role"] for m in store.messages(call["sessionId"])] == ["user"]


def test_end_during_tts_discards_transient_audio(store):
    call = start(store)
    loop, _ = runtime(store)

    class EndSpeech(Speech):
        def synthesize(self, text):
            result = super().synthesize(text)
            c.interrupt(store, call["id"], c.Revision(expected_revision=1), ended=True)
            return result

    result = send(store, loop, call, speech=EndSpeech())
    assert result["audio"] is None
    assert result["call"]["requests"][0]["textStatus"] == "complete"
    assert result["call"]["requests"][0]["speechStatus"] == "failed-or-interrupted"


def test_owner_only_api_factory_capabilities_and_typed_request(store):
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient

    from pi.calls_api import router

    def owner(key: str | None = Header(default=None)):
        if key != "owner":
            raise HTTPException(403)

    loop, provider = runtime(store)
    app = FastAPI()
    app.include_router(router(lambda: store, lambda: loop, owner))
    client = TestClient(app)
    assert client.get("/calls/capabilities").status_code == 403
    capabilities = client.get("/calls/capabilities", headers={"key": "owner"})
    assert capabilities.status_code == 200
    assert capabilities.json()["camera"] == "unavailable"
    source = store.create_session()
    call = client.post(
        "/calls",
        headers={"key": "owner"},
        json={"request_id": "api_start_request_8573", "conversationId": source},
    )
    assert call.status_code == 200
    result = client.post(
        f"/calls/{call.json()['id']}/turns",
        headers={"key": "owner"},
        json={"request_id": "api_turn_request_8573", "text": "hello"},
    )
    assert result.status_code == 200
    assert result.json()["call"]["requests"][0]["textStatus"] == "complete"
    assert len(provider.seen) == 1
