"""Durable schedule admission; dispatch never reconstructs a published procedure."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from .agents import StrictModel


class Timing(StrictModel):
    kind: Literal["daily", "weekly", "interval"]
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    day: int = Field(ge=0, le=6)
    hours: int = Field(ge=1, le=168)


class Target(StrictModel):
    kind: Literal["tool", "automation"]
    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.-]+$")
    publishedVersion: int = Field(ge=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    args: dict

    @field_validator("args")
    @classmethod
    def bounded(cls, value):
        if len(json.dumps(value, allow_nan=False)) > 100000:
            raise ValueError("Job inputs exceed 100000 characters.")
        return value


class Definition(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=4000)
    agentId: str = Field(min_length=1, max_length=200)
    timing: Timing
    timeZone: str = Field(min_length=1, max_length=100)
    enabled: bool
    target: Target
    overlap: Literal["skip"] = "skip"
    requireBudget: bool = False

    @field_validator("timeZone")
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("Use an available IANA time zone.") from exc
        return value

    @field_validator("name", "instructions", "agentId")
    @classmethod
    def text(cls, value):
        if not value.strip():
            raise ValueError("Provide nonempty text.")
        return value.strip()


class Update(StrictModel):
    expected_revision: int = Field(ge=1)
    definition: Definition


SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_jobs (
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL, definition TEXT NOT NULL,
 created_at REAL NOT NULL, next_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled_runs (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES scheduled_jobs(id),
 job_revision INTEGER NOT NULL, definition TEXT NOT NULL, scheduled_at REAL NOT NULL,
 started_at REAL NOT NULL, status TEXT NOT NULL, receipt TEXT, request_id TEXT UNIQUE,
 UNIQUE(job_id,scheduled_at,request_id)
);
CREATE TABLE IF NOT EXISTS scheduled_run_budgets (
 run_id TEXT PRIMARY KEY REFERENCES scheduled_runs(id), budget_id TEXT NOT NULL UNIQUE,
 bound_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS scheduled_once ON scheduled_runs(job_id,scheduled_at)
 WHERE request_id IS NULL;
"""


class JobError(RuntimeError):
    pass


def next_due(definition, after, *, anchor):
    """Strictly after UTC instant. DST missing slots skip; repeated slots run once."""
    if definition.timing.kind == "interval":
        step = definition.timing.hours * 3600
        return anchor + (max(0, int((after - anchor) // step)) + 1) * step
    zone = ZoneInfo(definition.timeZone)
    day = datetime.fromtimestamp(after, UTC).astimezone(zone).date()
    hour, minute = map(int, definition.timing.time.split(":"))
    for offset in range(15):
        date = day + timedelta(days=offset)
        if definition.timing.kind == "weekly" and (date.weekday() + 1) % 7 != definition.timing.day:
            continue
        local = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone, fold=0)
        utc = local.astimezone(UTC)
        if utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            continue
        if utc.timestamp() > after:
            return utc.timestamp()
    raise JobError("No valid scheduled slot in the next fifteen days.")


def _row(db, identity):
    row = db.execute("SELECT * FROM scheduled_jobs WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise JobError("Job not found.")
    return row


def _view(row):
    return {**dict(row), "definition": json.loads(row["definition"])}


def list_jobs(store):
    with store._connect() as db:
        return [
            _view(row) for row in db.execute("SELECT * FROM scheduled_jobs ORDER BY created_at,id")
        ]


def create(store, definition, now=None):
    definition = Definition.model_validate(definition.model_dump())
    now = time.time() if now is None else now
    identity = "job_" + uuid.uuid4().hex
    with store._connect() as db:
        db.execute(
            "INSERT INTO scheduled_jobs VALUES (?,1,?,?,?)",
            (identity, definition.model_dump_json(), now, next_due(definition, now, anchor=now)),
        )
        return _view(_row(db, identity))


def update(store, identity, body, now=None):
    body = Update.model_validate(body.model_dump())
    now = time.time() if now is None else now
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = _row(db, identity)
        if old["revision"] != body.expected_revision:
            raise JobError("Job changed; reload before saving.")
        due = next_due(body.definition, now, anchor=old["created_at"])
        db.execute(
            "UPDATE scheduled_jobs SET revision=revision+1,definition=?,next_at=? WHERE id=?",
            (body.definition.model_dump_json(), due, identity),
        )
        result = _view(_row(db, identity))
        db.commit()
        return result


def _claim(db, row, now, request_id=None):
    if db.execute(
        "SELECT 1 FROM scheduled_runs WHERE job_id=? "
        "AND status NOT IN ('completed','failed','cancelled')",
        (row["id"],),
    ).fetchone():
        return None
    run = "scheduled_" + uuid.uuid4().hex
    scheduled = now if request_id else row["next_at"]
    db.execute(
        "INSERT INTO scheduled_runs VALUES (?,?,?,?,?,?,'ready',NULL,?)",
        (run, row["id"], row["revision"], row["definition"], scheduled, now, request_id),
    )
    if json.loads(row["definition"]).get("requireBudget", False):
        db.execute("UPDATE scheduled_runs SET status='awaiting_budget' WHERE id=?", (run,))
    return {
        "id": run,
        "job_id": row["id"],
        "definition": json.loads(row["definition"]),
        "scheduled_at": scheduled,
    }


def claim_due(store, now=None, limit=20):
    now = time.time() if now is None else now
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Use a limit of 1-100 jobs.")
    claimed = []
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT * FROM scheduled_jobs WHERE next_at<=? ORDER BY next_at,id LIMIT ?",
            (now, limit),
        ).fetchall()
        for row in rows:
            definition = Definition.model_validate_json(row["definition"])
            if definition.enabled:
                run = _claim(db, row, now)
                if run:
                    claimed.append(run)
            # Missed slots coalesce to one; overlap skips, never queues a burst.
            db.execute(
                "UPDATE scheduled_jobs SET next_at=? WHERE id=?",
                (next_due(definition, now, anchor=row["created_at"]), row["id"]),
            )
        db.commit()
    return claimed


def run_now(store, identity, request_id, now=None):
    if not isinstance(request_id, str) or not 16 <= len(request_id) <= 128:
        raise ValueError("Use a stable 16-128 character request ID.")
    now = time.time() if now is None else now
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT * FROM scheduled_runs WHERE request_id=?", (request_id,)
        ).fetchone()
        if old:
            if old["job_id"] != identity:
                raise JobError("Request ID belongs to a different job.")
            return {**dict(old), "definition": json.loads(old["definition"]), "replayed": True}
        row = _row(db, identity)
        run = _claim(db, row, now, request_id)
        if run is None:
            raise JobError("Resolve the current run before starting another.")
        db.commit()
        return {**run, "replayed": False}


def finish(store, identity, status, receipt):
    if status not in ("completed", "failed", "awaiting_approval", "outcome_unknown"):
        raise ValueError("Unsupported run outcome.")
    encoded = json.dumps(receipt, allow_nan=False)
    if len(encoded) > 100000:
        raise ValueError("Receipt exceeds limit.")
    with store._connect() as db:
        changed = db.execute(
            "UPDATE scheduled_runs SET status=?,receipt=? WHERE id=? AND status='dispatching'",
            (status, encoded, identity),
        ).rowcount
        if not changed:
            raise JobError("Run is not awaiting a dispatch result.")


def runs(store, identity):
    with store._connect() as db:
        _row(db, identity)
        return [
            {
                **dict(row),
                "definition": json.loads(row["definition"]),
                "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
            }
            for row in db.execute(
                "SELECT r.*, b.budget_id AS spending_budget_id FROM scheduled_runs r "
                "LEFT JOIN scheduled_run_budgets b ON b.run_id=r.id "
                "WHERE r.job_id=? ORDER BY r.started_at DESC LIMIT 100",
                (identity,),
            )
        ]


def dispatch_claim(store, run, invoke, *, resume=False):
    """Injected server adapter invokes the exact publication; never retries an uncertain effect."""
    # Acquire the dispatch right before invoking. Only identity comes from the
    # caller; a stale or modified claim cannot replace the stored snapshot.
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        saved = db.execute("SELECT * FROM scheduled_runs WHERE id=?", (run["id"],)).fetchone()
        if saved is None:
            raise JobError("Run not found.")
        expected = "awaiting_approval" if resume else "ready"
        if saved["status"] != expected:
            return saved["status"]
        approval = {}
        if resume:
            receipt = json.loads(saved["receipt"] or "{}")
            request_id = receipt.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                raise JobError("Run has no saved approval request.")
            approval["approval_request_id"] = request_id
        db.execute("UPDATE scheduled_runs SET status='dispatching' WHERE id=?", (saved["id"],))
        definition = json.loads(saved["definition"])
        budget = db.execute(
            "SELECT budget_id FROM scheduled_run_budgets WHERE run_id=?", (saved["id"],)
        ).fetchone()
        if definition.get("requireBudget", False) and not budget:
            raise JobError("Bind an owner budget before dispatch.")
        if budget:
            approval["spending_job_id"] = budget["budget_id"]
        db.commit()
    try:
        outcome = invoke(
            definition["target"], action_id=saved["id"], agent_id=definition["agentId"], **approval
        )
        status = outcome.get("status")
        if status not in ("completed", "failed", "awaiting_approval"):
            status = "outcome_unknown"
        if len(json.dumps(outcome, allow_nan=False)) > 100000:
            raise ValueError("Receipt exceeds limit.")
    except Exception:
        # Provider/transport exceptions may have happened after the effect.
        status = "outcome_unknown"
        outcome = {"reason": "Dispatch outcome is unconfirmed; reconcile before another run."}
    finish(store, saved["id"], status, outcome)
    return status


def reconcile(store, identity, adapter):
    """Resolve held dispatches from authoritative receipts, without invoking again."""
    with store._connect() as db:
        row = db.execute("SELECT * FROM scheduled_runs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise JobError("Run not found.")
        if row["status"] not in ("dispatching", "outcome_unknown"):
            return row["status"]
        definition = json.loads(row["definition"])
    outcome = adapter.reconcile(
        definition["target"], action_id=identity, agent_id=definition["agentId"]
    )
    status = outcome.get("status")
    if status not in ("completed", "failed"):
        return row["status"]
    encoded = json.dumps(outcome, allow_nan=False)
    if len(encoded) > 100000:
        raise JobError("Receipt exceeds limit.")
    with store._connect() as db:
        db.execute(
            "UPDATE scheduled_runs SET status=?,receipt=? WHERE id=? "
            "AND status IN ('dispatching','outcome_unknown')",
            (status, encoded, identity),
        )
        return db.execute("SELECT status FROM scheduled_runs WHERE id=?", (identity,)).fetchone()[0]


class BudgetBinding(StrictModel):
    budget_id: str = Field(pattern=r"^job_[a-f0-9]{32}$")


def bind_budget(store, identity, body, adapter):
    body = BudgetBinding.model_validate(body.model_dump())
    with store._connect() as db:
        row = db.execute("SELECT * FROM scheduled_runs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise JobError("Run not found.")
        definition = json.loads(row["definition"])
    if not adapter.validate_budget(
        body.budget_id, action_id=identity, agent_id=definition["agentId"]
    ):
        raise JobError("Budget unavailable or does not belong to this agent and run.")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT budget_id FROM scheduled_run_budgets WHERE run_id=?", (identity,)
        ).fetchone()
        if existing:
            if existing["budget_id"] != body.budget_id:
                raise JobError("This run already has a different budget.")
            return {"run_id": identity, "budget_id": body.budget_id, "replayed": True}
        row = db.execute("SELECT status FROM scheduled_runs WHERE id=?", (identity,)).fetchone()
        if row["status"] != "awaiting_budget":
            raise JobError("Only a run waiting for its budget can be bound.")
        if db.execute(
            "SELECT 1 FROM scheduled_run_budgets WHERE budget_id=?", (body.budget_id,)
        ).fetchone():
            raise JobError("Budget already belongs to another run.")
        db.execute(
            "INSERT INTO scheduled_run_budgets VALUES (?,?,?)",
            (identity, body.budget_id, time.time()),
        )
        db.execute("UPDATE scheduled_runs SET status='ready' WHERE id=?", (identity,))
        db.commit()
        return {"run_id": identity, "budget_id": body.budget_id, "replayed": False}


def cancel_run(store, identity):
    """Withdraw undispatched work; never claim an uncertain effect was cancelled."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM scheduled_runs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise JobError("Run not found.")
        if row["status"] == "cancelled":
            return {"status": "cancelled", "replayed": True}
        if row["status"] not in ("ready", "awaiting_budget", "awaiting_approval"):
            raise JobError("Only waiting work can be cancelled; reconcile dispatched work first.")
        # Preserve any saved approval receipt and spending binding as evidence.
        db.execute("UPDATE scheduled_runs SET status='cancelled' WHERE id=?", (identity,))
        db.commit()
        return {"status": "cancelled", "replayed": False}
