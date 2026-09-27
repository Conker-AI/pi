"""Durable, content-free evidence that the selected setup model answered once."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import model_roles
from .providers import Message, ProviderUnavailable

REQUEST_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}")
PROBE_PROMPT = "Reply briefly with the word ready."
PROBE_TIMEOUT_SECONDS = 60.0
MAX_RESPONSE_CHARACTERS = 16_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS setup_model_probe_requests (
    request_id             TEXT PRIMARY KEY,
    configuration_revision INTEGER NOT NULL CHECK(configuration_revision > 0),
    candidate_id           TEXT NOT NULL,
    state                  TEXT NOT NULL CHECK(state IN ('started','passed','failed')),
    failure_code           TEXT,
    started_at             REAL NOT NULL,
    completed_at           REAL
);
CREATE TABLE IF NOT EXISTS setup_model_probe_receipts (
    request_id             TEXT PRIMARY KEY REFERENCES setup_model_probe_requests(request_id),
    configuration_revision INTEGER NOT NULL CHECK(configuration_revision > 0),
    candidate_id           TEXT NOT NULL,
    provider_id            TEXT NOT NULL,
    requested_model        TEXT NOT NULL,
    actual_model           TEXT NOT NULL,
    execution              TEXT NOT NULL CHECK(execution IN ('local','hosted')),
    response_digest        TEXT NOT NULL,
    completed_at           REAL NOT NULL,
    recorded_at            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS setup_model_probe_receipts_configuration
    ON setup_model_probe_receipts(configuration_revision, candidate_id, completed_at DESC);
CREATE TRIGGER IF NOT EXISTS setup_model_probe_receipts_no_update
BEFORE UPDATE ON setup_model_probe_receipts BEGIN
    SELECT RAISE(ABORT, 'setup model probe receipts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS setup_model_probe_receipts_no_delete
BEFORE DELETE ON setup_model_probe_receipts BEGIN
    SELECT RAISE(ABORT, 'setup model probe receipts are append-only');
END;
"""


class ProbeError(Exception):
    def __init__(self, code: str, detail: str, status: int = 422):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(detail)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProbeInput(StrictModel):
    requestId: str = Field(min_length=1, max_length=128)
    candidateId: str = Field(min_length=1, max_length=200)
    expectedRevision: int = Field(gt=0)


class ProbeReceipt(StrictModel):
    schemaVersion: Literal[1] = 1
    requestId: str
    configurationRevision: int = Field(gt=0)
    candidateId: str
    providerId: str
    requestedModel: str
    actualModel: str
    execution: Literal["local", "hosted"]
    responseDigest: str
    completedAt: datetime
    recordedAt: datetime


def initialize(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def _utc(value: float) -> datetime:
    return datetime.fromtimestamp(value, UTC)


def _project(row: sqlite3.Row) -> ProbeReceipt:
    return ProbeReceipt(
        requestId=row["request_id"],
        configurationRevision=row["configuration_revision"],
        candidateId=row["candidate_id"],
        providerId=row["provider_id"],
        requestedModel=row["requested_model"],
        actualModel=row["actual_model"],
        execution=row["execution"],
        responseDigest=row["response_digest"],
        completedAt=_utc(row["completed_at"]),
        recordedAt=_utc(row["recorded_at"]),
    )


def current(store, configuration_revision: int, candidate_id: str) -> ProbeReceipt | None:
    with store._connect() as db:
        row = db.execute(
            "SELECT * FROM setup_model_probe_receipts "
            "WHERE configuration_revision=? AND candidate_id=? "
            "ORDER BY completed_at DESC LIMIT 1",
            (configuration_revision, candidate_id),
        ).fetchone()
    return _project(row) if row else None


def _selected(saved: dict, candidate_id: str) -> tuple[dict, dict]:
    configuration = saved["configuration"]
    if configuration is None:
        raise ProbeError("model_configuration_missing", "Choose an answer model before testing it.")
    answer = configuration["roleSettings"]["roles"]["answer"]
    if answer["modelId"] != candidate_id:
        raise ProbeError(
            "model_selection_changed", "The selected answer model changed; reload setup.", 409
        )
    model = next((item for item in configuration["models"] if item["id"] == candidate_id), None)
    if model is None or not model["enabled"]:
        raise ProbeError("model_selection_unavailable", "The selected answer model is unavailable.")
    provider = next(
        (item for item in configuration["providers"] if item["id"] == model["providerId"]),
        None,
    )
    if provider is None or not provider["enabled"]:
        raise ProbeError(
            "model_selection_unavailable", "The selected answer-model provider is unavailable."
        )
    return model, provider


def _claim(store, body: ProbeInput, now: datetime) -> ProbeReceipt | None:
    if not REQUEST_ID.fullmatch(body.requestId):
        raise ProbeError("invalid_request_id", "requestId must be a bounded machine identity.")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute(
            "SELECT * FROM setup_model_probe_requests WHERE request_id=?", (body.requestId,)
        ).fetchone()
        if prior:
            same = (
                prior["configuration_revision"] == body.expectedRevision
                and prior["candidate_id"] == body.candidateId
            )
            if not same:
                db.execute("ROLLBACK")
                raise ProbeError(
                    "replay_conflict", "requestId was already used for another model probe.", 409
                )
            if prior["state"] == "passed":
                receipt = db.execute(
                    "SELECT * FROM setup_model_probe_receipts WHERE request_id=?",
                    (body.requestId,),
                ).fetchone()
                db.execute("COMMIT")
                if receipt is None:
                    raise ProbeError(
                        "receipt_unavailable", "The saved model probe receipt is unavailable.", 503
                    )
                return _project(receipt)
            db.execute("ROLLBACK")
            detail = (
                "This model probe has an uncertain outcome; start a new explicit test."
                if prior["state"] == "started"
                else "This model probe already failed; start a new explicit test."
            )
            raise ProbeError("probe_not_replayable", detail, 409)
        db.execute(
            "INSERT INTO setup_model_probe_requests VALUES (?,?,?,?,?,?,?)",
            (
                body.requestId,
                body.expectedRevision,
                body.candidateId,
                "started",
                None,
                now.timestamp(),
                None,
            ),
        )
        db.execute("COMMIT")
    return None


def _failed(store, request_id: str, code: str, now: datetime) -> None:
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE setup_model_probe_requests SET state='failed',failure_code=?,completed_at=? "
            "WHERE request_id=? AND state='started'",
            (code, now.timestamp(), request_id),
        )
        db.execute("COMMIT")


def probe(store, router, body: ProbeInput, *, now: datetime | None = None) -> ProbeReceipt:
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    saved = model_roles.load(store)
    if saved["revision"] != body.expectedRevision:
        raise ProbeError("revision_conflict", "Model settings changed; reload before testing.", 409)
    model, _provider = _selected(saved, body.candidateId)
    adapters = router.adapters()
    adapter = adapters.get(model["providerId"])
    if adapter is None or not callable(getattr(adapter, "complete_bounded", None)):
        raise ProbeError(
            "provider_unavailable", "The selected answer-model provider cannot be tested.", 503
        )
    replay = _claim(store, body, observed)
    if replay is not None:
        return replay

    try:
        completion = adapter.complete_bounded(
            [Message("user", PROBE_PROMPT)],
            model=model["route"],
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except ProviderUnavailable:
        _failed(store, body.requestId, "provider_unavailable", datetime.now(UTC))
        raise ProbeError(
            "provider_unavailable", "The selected answer model did not answer the setup test.", 503
        ) from None
    except Exception:
        _failed(store, body.requestId, "provider_error", datetime.now(UTC))
        raise ProbeError(
            "provider_error", "The selected answer model could not complete the setup test.", 503
        ) from None

    text = completion.text.strip() if isinstance(completion.text, str) else ""
    actual = completion.model.strip() if isinstance(completion.model, str) else ""
    if not text or len(text) > MAX_RESPONSE_CHARACTERS or not actual or len(actual) > 300:
        _failed(store, body.requestId, "invalid_response", datetime.now(UTC))
        raise ProbeError(
            "invalid_response", "The selected answer model returned an invalid setup response."
        )
    execution = "local" if adapter is getattr(router, "local", None) else "hosted"
    completed = datetime.now(UTC)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO setup_model_probe_receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                body.requestId,
                body.expectedRevision,
                body.candidateId,
                model["providerId"],
                model["route"],
                actual,
                execution,
                digest,
                completed.timestamp(),
                completed.timestamp(),
            ),
        )
        db.execute(
            "UPDATE setup_model_probe_requests SET state='passed',completed_at=? "
            "WHERE request_id=? AND state='started'",
            (completed.timestamp(), body.requestId),
        )
        row = db.execute(
            "SELECT * FROM setup_model_probe_receipts WHERE request_id=?", (body.requestId,)
        ).fetchone()
        db.execute("COMMIT")
    return _project(row)
