from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pi import calls
from pi.browser_contract import owner_allowed, runtime_allowed, session_only_write
from pi.calls_api import router
from pi.loop import Loop
from pi.providers import Completion
from pi.routing import Router
from pi.store import Store


class Provider:
    name = "synthetic"

    def complete(self, messages, *, model):
        return Completion(text="Durable typed answer", model=model, provider=self.name)

    def complete_bounded(self, messages, *, model, timeout):
        return self.complete(messages, model=model)


class Speech:
    def __init__(self):
        self.stt = self.tts = 0

    def capabilities(self):
        return {"stt": {"status": "configured"}, "tts": {"status": "configured"}}

    def synthesize(self, text):
        self.tts += 1
        return {
            "audio": b"must cross only the original browser audio response",
            "mime": "audio/wav",
            "duration_seconds": 0.25,
        }

    def transcribe(self, audio, mime):
        self.stt += 1
        assert audio == b"synthetic wav" and mime == "audio/wav"
        return {
            "text": "Spoken owner turn",
            "segments": None,
            "words": None,
            "duration_seconds": 0.5,
        }


def test_exact_owner_allowlist_and_session_only_writes():
    identity = "call_" + "a" * 32
    reads = (
        "/calls/browser/capabilities",
        "/calls/browser/active/ses_source_123456",
        f"/calls/browser/{identity}",
    )
    writes = (
        "/calls/browser",
        f"/calls/browser/{identity}/update",
        f"/calls/browser/{identity}/interrupt",
        f"/calls/browser/{identity}/end",
        f"/calls/browser/{identity}/turns",
        f"/calls/browser/{identity}/audio",
    )
    for path in reads:
        assert owner_allowed("GET", path) and not runtime_allowed("GET", path)
    for path in writes:
        assert owner_allowed("POST", path) and not runtime_allowed("POST", path)
        assert session_only_write("POST", path)
    for method, path in (
        ("GET", "/calls"),
        ("GET", "/calls/capabilities"),
        ("POST", f"/calls/{identity}/turns"),
        ("POST", f"/calls/{identity}/audio"),
        ("DELETE", f"/calls/browser/{identity}"),
    ):
        assert not owner_allowed(method, path)
        assert not session_only_write(method, path)


def test_browser_call_lifecycle_is_typed_bounded_and_reconnectable(tmp_path):
    store = Store(tmp_path / "calls.db")
    loop = Loop(store, Router(local_provider=Provider(), local_model="synthetic"))
    speech = Speech()

    def owner(x_owner: str | None = Header(default=None)):
        if x_owner != "owner":
            raise HTTPException(401)

    app = FastAPI()
    app.include_router(router(lambda: store, lambda: loop, owner, lambda: speech))
    source = store.create_session()
    headers = {"X-Owner": "owner"}
    with TestClient(app) as client:
        assert client.get("/calls/browser/capabilities").status_code == 401
        available = client.get("/calls/browser/capabilities", headers=headers)
        assert available.status_code == 200
        assert available.headers["cache-control"] == "no-store"
        assert available.json()["speechInput"] == "configured"
        assert client.get("/calls/browser/capabilities?probe=1", headers=headers).status_code == 422

        started = client.post(
            "/calls/browser",
            headers=headers,
            json={"request_id": "browser_call_start_01", "conversationId": source},
        )
        assert started.status_code == 200, started.text
        call = started.json()
        assert call["schemaVersion"] == 1 and call["authority"] == "none"
        assert call["execution"] == "typed-and-audio-turns" and call["audioIncluded"] is False
        assert call["retention"] == "transcript-persisted; raw-media-none"
        identity = call["id"]

        active = client.get(f"/calls/browser/active/{source}", headers=headers)
        assert active.status_code == 200 and active.json()["id"] == identity
        assert client.get(f"/calls/browser/{identity}", headers=headers).json()["id"] == identity

        updated = client.post(
            f"/calls/browser/{identity}/update",
            headers=headers,
            json={"expected_revision": call["revision"], "channels": {"microphone": True}},
        ).json()
        assert updated["channels"]["microphone"] is True

        turn = client.post(
            f"/calls/browser/{identity}/turns",
            headers=headers,
            json={"request_id": "browser_call_turn_001", "text": "Talk this through with me."},
        )
        assert turn.status_code == 200, turn.text
        result = turn.json()
        assert result["execution"] == "typed-turn" and result["audioIncluded"] is False
        assert result["transcriptionIncluded"] is False and speech.tts == 0
        assert "audio" not in result and "transcription" not in result
        assert any(event["text"] == "Durable typed answer" for event in result["call"]["events"])

        audio = client.post(
            f"/calls/browser/{identity}/audio?request_id=browser_call_audio_001",
            headers={**headers, "Content-Type": "audio/wav"},
            content=b"synthetic wav",
        )
        assert audio.status_code == 200, audio.text
        voice = audio.json()
        assert voice["execution"] == "audio-turn"
        assert voice["audioIncluded"] and voice["transcriptionIncluded"]
        assert voice["audio"]["retention"] == "transient-response-only"
        assert voice["transcription"] == {
            "text": "Spoken owner turn",
            "durationSeconds": 0.5,
            "timing": "unavailable",
            "retention": "transient-response-only",
        }
        assert (speech.stt, speech.tts) == (1, 1)
        replay = client.post(
            f"/calls/browser/{identity}/audio?request_id=browser_call_audio_001",
            headers={**headers, "Content-Type": "audio/wav"},
            content=b"synthetic wav",
        ).json()
        assert replay["replayed"] and not replay["audioIncluded"]
        assert (speech.stt, speech.tts) == (1, 1)

        interrupted = client.post(
            f"/calls/browser/{identity}/interrupt",
            headers=headers,
            json={"expected_revision": result["call"]["revision"]},
        ).json()
        ended = client.post(
            f"/calls/browser/{identity}/end",
            headers=headers,
            json={"expected_revision": interrupted["revision"]},
        ).json()
        assert ended["phase"] == "ended" and ended["endedAt"] is not None
        assert client.get(f"/calls/browser/active/{source}", headers=headers).status_code == 404
    store.close()


def test_browser_projection_keeps_only_latest_bounded_history(tmp_path):
    store = Store(tmp_path / "calls.db")
    source = store.create_session()
    value = calls.start(
        store, calls.Start(request_id="bounded_call_start_01", conversationId=source)
    )
    value["events"] = [
        {"id": f"call_event_{index}", "at": float(index), "kind": "event", "text": "status"}
        for index in range(205)
    ]
    value["requests"] = [
        {
            "requestId": f"bounded_request_{index:03d}",
            "state": "complete",
            "inputKind": "text",
            "speechStatus": "not-requested",
            "errorCode": None,
            "turnId": None,
            "turnStatus": None,
            "textStatus": "complete",
            "acted": False,
            "messageIds": [],
        }
        for index in range(105)
    ]
    projected = calls.browser_view(value)
    assert projected.eventsTruncated and projected.requestsTruncated
    assert len(projected.events) == 200 and projected.events[0].id == "call_event_5"
    assert len(projected.requests) == 100 and projected.requests[0].requestId.endswith("005")
    store.close()
