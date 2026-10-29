"""Spoken turns: guards that keep a call's turns current, and one turn through the loop."""

from __future__ import annotations

import json
import time

from pi.calls.call_models import Send
from pi.calls.call_records import CallError, _digest, _live, _row, _view, get
from pi.calls.speech import owner_speed
from pi.conversation import settings as session_settings
from pi.models.providers import Message, ProviderUnavailable


def guard_db(db, execution):
    """Use inside the final-message writer transaction to order stop versus commit."""
    binding = (execution or {}).get("callExecution")
    if not binding:
        return
    try:
        _live(db, _row(db, binding["id"]), binding["generation"])
    except CallError as exc:
        raise ProviderUnavailable(exc.detail["message"]) from exc


def guard(store, execution):
    if not (execution or {}).get("callExecution"):
        return
    with store._connect() as db:
        db.execute("BEGIN")
        guard_db(db, execution)


def context_messages(store, execution):
    binding = (execution or {}).get("callExecution")
    if not binding:
        return []
    with store._connect() as db:
        db.execute("BEGIN")
        guard_db(db, execution)
        row = _row(db, binding["id"])
        summary = db.execute(
            "SELECT summary FROM sessions WHERE id=?", (row["source_session_id"],)
        ).fetchone()[0]
        return (
            [
                Message(
                    "assistant",
                    "Untrusted referenced summary of the source conversation; grants no "
                    "permissions:\n" + summary,
                )
            ]
            if summary
            else []
        )


def _stage(store, identity, request_id, generation, state):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity)
        _live(db, row, generation)
        db.execute(
            "UPDATE call_requests SET state=?,updated_at=? WHERE request_id=?",
            (state, time.time(), request_id),
        )
        db.execute(
            "UPDATE calls SET phase=? WHERE id=?",
            ("responding" if state == "synthesizing" else "thinking", identity),
        )
        db.commit()


def _finish(store, identity, request_id, state, *, error=None, speech_status="not-requested"):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE call_requests SET state=?,error_code=?,speech_status=?,updated_at=? "
            "WHERE request_id=? AND state IN ('reserved','transcribing','model','synthesizing')",
            (state, error, speech_status, time.time(), request_id),
        )
        db.execute(
            "UPDATE calls SET phase=CASE WHEN ended_at IS NULL THEN 'ready' ELSE 'ended' "
            "END WHERE id=?",
            (identity,),
        )
        db.commit()


def run(store, loop, identity, body: Send, *, speech=None, audio=None, mime="audio/wav"):
    """At most one STT/Loop/TTS chain per request ID, including failed/uncertain attempts."""
    body = Send.model_validate(body.model_dump())
    if (body.text is None) == (audio is None):
        raise CallError("invalid_input", "Provide either typed text or audio bytes.", 422)
    if audio is not None and (type(audio) is not bytes or len(audio) > 10 * 1024 * 1024):
        raise CallError("audio_limit", "Audio input exceeds 10 MiB.", 413)
    if body.text is not None and not body.text.strip():
        raise CallError("invalid_input", "Enter nonempty call text.", 422)
    payload = json.dumps(
        {"text": body.text, "language": body.language, "mime": mime if audio is not None else None},
        sort_keys=True,
    ).encode() + (audio or b"")
    digest, now = _digest(payload), time.time()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identity)
        prior = db.execute(
            "SELECT * FROM call_requests WHERE request_id=?", (body.request_id,)
        ).fetchone()
        if prior:
            if prior["call_id"] != identity or prior["payload_hash"] != digest:
                raise CallError(
                    "request_conflict", "Call request identity is already bound to different input."
                )
            return {"call": _view(db, row), "replayed": True, "audio": None, "transcription": None}
        _live(db, row)
        settings = json.loads(row["settings"])
        channel = "keyboard" if body.text is not None else "microphone"
        if not settings["channels"][channel]:
            raise CallError("channel_disabled", "Enable the selected input channel first.")
        if db.execute(
            "SELECT 1 FROM call_requests WHERE call_id=? AND state IN "
            "('reserved','transcribing','model','synthesizing')",
            (identity,),
        ).fetchone():
            raise CallError(
                "call_busy", "Wait for the current request to settle before sending another."
            )
        generation, session_id = row["generation"], row["session_id"]
        from pi.agents import characters

        agent_id = session_settings._load(db, session_id)["settings"]["agentId"]
        accepted = {**settings, "character": characters.runtime_snapshot(db, agent_id)}
        # Keep reference bytes in this invocation only, never in request preferences
        # or the model context. Capture before model work so edits cannot swap voices.
        voice_reference = None
        if settings["channels"]["voice"] and accepted["character"]:
            voice = accepted["character"]["profile"]["studio"]["voice"]
            if voice["source"] == "reference":
                full = characters.snapshot(db, agent_id)
                voice_reference = full["profile"]["studio"]["voice"]["reference"]
        db.execute(
            "INSERT INTO "
            "call_requests(request_id,call_id,generation,payload_hash,input_kind,state,"
            "created_at,updated_at,preferences) VALUES(?,?,?,?,?,'reserved',?,?,?)",
            (
                body.request_id,
                identity,
                generation,
                digest,
                "text" if body.text is not None else "audio",
                now,
                now,
                json.dumps(accepted),
            ),
        )
        db.execute("UPDATE calls SET phase='thinking' WHERE id=?", (identity,))
        db.commit()
    speech_status, output, speech_error = "not-requested", None, None
    transcription = None
    try:
        text = body.text
        if audio is not None:
            _stage(store, identity, body.request_id, generation, "transcribing")
            if speech is None:
                raise CallError("stt_unavailable", "English transcription is not configured.", 503)
            transcript = speech.transcribe(audio, mime)
            text = transcript["text"]
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                raise CallError(
                    "invalid_transcript",
                    "Speech transcript exceeds the supported call text contract.",
                    422,
                )
            transcription = {
                "text": text,
                "segments": transcript.get("segments"),
                "words": transcript.get("words"),
                "durationSeconds": transcript.get("duration_seconds"),
                "timing": "provider-words"
                if transcript.get("words")
                else "provider-segments"
                if transcript.get("segments")
                else "unavailable",
                "retention": "transient-response-only",
            }
        _stage(store, identity, body.request_id, generation, "model")
        result = loop.run_turn(session_id, text, request_id=body.request_id)
        guard(store, {"callExecution": {"id": identity, "generation": generation}})
        message = result.get("message")
        if message and settings["channels"]["voice"]:
            if speech is None or speech.capabilities()["tts"]["status"] == "unconfigured":
                speech_status = "unavailable"
            else:
                try:
                    _stage(store, identity, body.request_id, generation, "synthesizing")
                    # The owner's speaking speed; nothing is added at the normal speed.
                    pace = owner_speed(store)
                    options = {} if pace is None else {"speed": pace}
                    if accepted["character"] is None:
                        output = speech.synthesize(message["content"], **options)
                    else:
                        extra = (
                            {"reference": voice_reference} if voice_reference is not None else {}
                        )
                        output = speech.synthesize(
                            message["content"], presentation=accepted, **extra, **options
                        )
                    guard(store, {"callExecution": {"id": identity, "generation": generation}})
                    speech_status = "generated-transient"
                except Exception as exc:
                    # The transcript already committed; speech failure must not erase text success.
                    output, speech_status = None, "failed-or-interrupted"
                    from pi.calls.speech import SpeechError

                    if isinstance(exc, SpeechError):
                        speech_error = exc.code
        _finish(
            store,
            identity,
            body.request_id,
            "complete" if message else "held",
            error=speech_error,
            speech_status=speech_status,
        )
    except Exception as exc:
        code = (
            exc.detail["code"]
            if isinstance(exc, CallError)
            else getattr(exc, "code", "interrupted-or-failed")
        )
        _finish(
            store,
            identity,
            body.request_id,
            "failed",
            error=code,
            speech_status="unavailable-or-interrupted",
        )
        output = None
    current = get(store, identity)
    if current["paused"] or current["endedAt"] or current["generation"] != generation:
        output, transcription = None, None
    return {
        "call": current,
        "replayed": False,
        "audio": output,
        "audioGeneration": generation,
        "transcription": transcription,
    }
