"""Owner-requested narration retries: durable identity, exact inputs, no tool dispatch."""

import json
import time
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from . import session_settings, submissions, tasks, turn_context, turn_control, turn_queue
from .providers import Message, ProviderUnavailable
from .routing import TurnContext

NARRATION = (
    "Regenerate only the final answer using the recorded context. "
    "Tool results are existing evidence. Do not request or repeat actions."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS response_retries (
 request_id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES sessions(id),
 source_message_id TEXT NOT NULL REFERENCES messages(id),
 source_turn_id TEXT NOT NULL REFERENCES turns(id),
 turn_id TEXT UNIQUE NOT NULL REFERENCES turns(id),
 model_id TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS response_retry_no_update BEFORE UPDATE ON response_retries
BEGIN SELECT RAISE(ABORT,'retry identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS response_retry_no_delete BEFORE DELETE ON response_retries
BEGIN SELECT RAISE(ABORT,'retry identity is permanent'); END;
CREATE TRIGGER IF NOT EXISTS response_retry_no_replace BEFORE INSERT ON response_retries
WHEN EXISTS(SELECT 1 FROM response_retries WHERE request_id=NEW.request_id)
BEGIN SELECT RAISE(ABORT,'retry identity is permanent'); END;
"""


class Retry(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    model_id: str = Field(min_length=1, max_length=200)


def _prior(db, sid, mid, body):
    tasks._source(db, sid)
    row = db.execute(
        "SELECT * FROM response_retries WHERE request_id=?", (body.request_id,)
    ).fetchone()
    if row and (
        row["session_id"] != sid
        or row["source_message_id"] != mid
        or row["model_id"] != body.model_id
    ):
        raise tasks.TaskError("request_conflict", "Retry request identity was already used.")
    return row


def receipt(store, sid, request_id, replayed=False):
    with store._connect() as db:
        tasks._source(db, sid)
        row = db.execute(
            "SELECT r.*,t.status FROM response_retries r JOIN turns t ON t.id=r.turn_id "
            "WHERE r.session_id=? AND r.request_id=?",
            (sid, request_id),
        ).fetchone()
        if row is None:
            raise tasks.TaskError("not_found", "Retry not found.", 404)
        final = db.execute(
            "SELECT message_id FROM turn_messages WHERE turn_id=? AND purpose='final'",
            (row["turn_id"],),
        ).fetchone()
        result = dict(row)
        source_turn = row["source_turn_id"]
        while previous := db.execute(
            "SELECT source_turn_id FROM response_retries WHERE turn_id=?", (source_turn,)
        ).fetchone():
            source_turn = previous[0]
        source_input = db.execute(
            "SELECT message_id FROM turn_messages WHERE turn_id=? AND purpose='input'",
            (source_turn,),
        ).fetchone()
        result["input_message_id"] = source_input[0] if source_input else None
    return {
        **result,
        "replayed": replayed,
        "message": store.get_message(final[0]) if final else None,
    }


def run(loop, sid, mid, body):
    body = Retry.model_validate(body.model_dump())
    store = loop.store
    with store._connect() as db:
        prior = _prior(db, sid, mid, body)
        source = db.execute(
            "SELECT tm.turn_id FROM turn_messages tm JOIN messages m ON m.id=tm.message_id "
            "WHERE tm.message_id=? AND m.session_id=? AND tm.purpose='final'",
            (mid, sid),
        ).fetchone()
    if prior:
        return receipt(store, sid, body.request_id, True)
    if source is None:
        raise tasks.TaskError(
            "retry_unavailable", "Choose a final answer in this conversation.", 404
        )
    prepared = turn_context.replay(store, source[0])
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = _prior(db, sid, mid, body)
        if prior:
            created = False
        else:
            tasks._source(db, sid, open_required=True)
            if (
                submissions._busy(db, sid)
                or db.execute(
                    "SELECT 1 FROM turn_submissions WHERE requested_session_id=? "
                    "AND state='preparing'",
                    (sid,),
                ).fetchone()
            ):
                raise tasks.TaskError("session_busy", "Resolve current work before retrying.")
            execution = dict(prepared["execution"])
            current = session_settings._load(db, sid)["settings"]["privacy"]
            if any(current[k] and not execution["privacy"][k] for k in current):
                raise tasks.TaskError(
                    "retry_privacy_changed", "Review context under the new privacy settings."
                )
            catalogue = db.execute(
                "SELECT revision,configuration FROM model_role_settings WHERE singleton=1"
            ).fetchone()
            execution["modelConfiguration"] = (
                json.loads(catalogue["configuration"]) if catalogue else None
            )
            execution["modelConfigurationRevision"] = catalogue["revision"] if catalogue else 0
            session_settings.validate_answer_model(execution, body.model_id)
            execution["answerModelId"] = body.model_id
            execution.pop("turnExecutionId", None)
            tid = "trn_" + uuid.uuid4().hex[:16]
            db.execute(
                "INSERT INTO turns(id,session_id,status,started_at) VALUES(?,?,'running',?)",
                (tid, sid, time.time()),
            )
            db.execute("INSERT INTO turn_settings VALUES(?,?)", (tid, json.dumps(execution)))
            db.execute(
                "INSERT INTO response_retries VALUES(?,?,?,?,?,?)",
                (body.request_id, sid, mid, source[0], tid, body.model_id),
            )
            saved = prepared["source"]
            prefix = list(saved["prefix"])
            if not any(m["role"] == "system" and m["content"] == NARRATION for m in prefix):
                prefix.insert(0, {"role": "system", "content": NARRATION})
            db.execute(
                "INSERT INTO turn_context_inputs VALUES(?,?,?,?,?)",
                (
                    tid,
                    json.dumps(saved["message_ids"]),
                    json.dumps(prefix),
                    saved["reply_to"],
                    time.time(),
                ),
            )
            turn_queue._source(db, sid)
            turn_queue._pause(db, sid, "response_retry")
            db.commit()
            created = True
    if not created:
        return receipt(store, sid, body.request_id, True)
    started = time.monotonic()
    try:
        execution = session_settings.execution(store, sid, tid)
        # Revalidate references after claiming. Replay contains the original tool
        # descriptions as historical context, but this path never dispatches tools.
        history = turn_context.replay(store, source[0])["messages"]
        if not any(m.role == "system" and m.content == NARRATION for m in history):
            history.insert(0, Message("system", NARRATION))
        from . import context_controls

        context_controls.check_budget(prepared["context"]["policy"], history)
        route, completion, skipped = loop._call(
            history,
            TurnContext(history_chars=loop._history_size(history), needs_tools=False),
            execution,
        )
        turn_control.guard(store, execution)
        loop._complete_turn(
            tid,
            completion.text,
            citations=completion.citations,
            provider=completion.provider,
            model=completion.model,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cached_tokens=completion.cached_tokens,
            cost_usd=completion.cost_usd,
            latency_ms=int((time.monotonic() - started) * 1000),
            route_tier=route.tier.value,
            route_reason=route.reason.value,
            detail="; ".join(skipped) or None,
        )
    except Exception:
        # Stable request identities inspect this failed/interrupted attempt; no
        # automatic provider retry occurs after failure or process restart.
        store.finish_turn(tid, "failed", detail="Narration retry did not complete.")
        raise
    return receipt(store, sid, body.request_id)


def router(get_loop, owner):
    api = APIRouter(dependencies=[Depends(owner)])

    @api.post("/sessions/{sid}/messages/{mid}/retry")
    def retry(sid: str, mid: str, body: Retry):
        from .loop import TurnFailed

        try:
            return run(get_loop(), sid, mid, body)
        except (ProviderUnavailable, TurnFailed):
            return receipt(get_loop().store, sid, body.request_id)

    @api.get("/sessions/{sid}/response-retries/{request_id}")
    def read(sid: str, request_id: str):
        return receipt(get_loop().store, sid, request_id)

    return api
