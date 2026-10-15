"""Content-free provider attempts; the original research ledger remains readable."""

import json
import math
import time
import uuid

FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "cost_usd")
METRICS = (*FIELDS, "summed_call_ms")
MAX_ATTEMPTS = 100
MAX_SAFE_NUMBER = 9_007_199_254_740_991
MAX_TOKEN_COUNT = 2**31 - 1
ROLES = {
    "answer",
    "thinking",
    "routing",
    "context-selection",
    "summarization",
    "memory-ranking",
    "search-ranking",
    "proposals",
}
EXTRA_COLUMNS = {
    "lane": "TEXT NOT NULL DEFAULT 'foreground'",
    "provider_id": "TEXT",
    "model_id": "TEXT",
    "actual_provider": "TEXT",
    "actual_model": "TEXT",
    "elapsed_ms": "INTEGER",
    "failure_kind": "TEXT",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS research_model_calls (
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
 provider TEXT NOT NULL, model TEXT NOT NULL, role TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','reported','failed')),
 started_at REAL NOT NULL, ended_at REAL,
 input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER, cost_usd REAL,
 lane TEXT NOT NULL DEFAULT 'foreground', provider_id TEXT, model_id TEXT,
 actual_provider TEXT, actual_model TEXT, elapsed_ms INTEGER, failure_kind TEXT
);
CREATE INDEX IF NOT EXISTS research_calls_turn ON research_model_calls(turn_id);
"""


def migrate(db):
    have = {row["name"] for row in db.execute("PRAGMA table_info(research_model_calls)")}
    for column, definition in EXTRA_COLUMNS.items():
        if column not in have:
            db.execute(f"ALTER TABLE research_model_calls ADD COLUMN {column} {definition}")


def _summary(db, turn_id, lane, covered):
    columns = {key: "elapsed_ms" if key == "summed_call_ms" else key for key in METRICS}
    valid = []
    for key, column in columns.items():
        limit = MAX_TOKEN_COUNT if key.endswith("_tokens") else MAX_SAFE_NUMBER
        kinds = "('integer','real')" if key == "cost_usd" else "('integer')"
        valid.append(
            f"CASE WHEN typeof({column}) IN {kinds} AND {column} BETWEEN 0 AND {limit} "
            f"THEN {column} END AS {column}"
        )
    values = db.execute(
        "SELECT COUNT(*) AS n,"
        "SUM(status='pending') AS pending,SUM(status='reported') AS reported,"
        "SUM(status='failed') AS failed,"
        + ",".join(
            f"TOTAL({column}) AS {key},COUNT({column}) AS {key}_known"
            for key, column in columns.items()
        )
        + " FROM (SELECT status,"
        + ",".join(valid)
        + " FROM research_model_calls WHERE turn_id=? AND lane=?)",
        (turn_id, lane),
    ).fetchone()
    count = values["n"]
    unknown = {key: count - values[key + "_known"] for key in METRICS}
    # TOTAL avoids SQLite integer overflow. Never emit a rounded unsafe integer,
    # infinity, or a fabricated partial subtotal if the full sum is unrepresentable.
    known = {
        key: (float(values[key]) if key == "cost_usd" else int(values[key]))
        if math.isfinite(values[key]) and 0 <= values[key] <= MAX_SAFE_NUMBER
        else None
        for key in METRICS
    }
    complete = {key: covered and unknown[key] == 0 and known[key] is not None for key in METRICS}
    return {
        "attempt_count": count,
        **{kind + "_count": values[kind] or 0 for kind in ("pending", "reported", "failed")},
        "totals": {key: known[key] if complete[key] else None for key in METRICS},
        "known_subtotal": known,
        "complete": complete,
        "unknown_counts": unknown,
    }


def totals(db, turn_id):
    """Preserve the research receipt's observed-attempt totals and nullable shape."""
    summary = _summary(db, turn_id, "foreground", True)
    return {key: summary["totals"][key] if summary["attempt_count"] else None for key in FIELDS}


def _accounted(db, turn_id):
    turn = db.execute("SELECT usage_version FROM turns WHERE id=?", (turn_id,)).fetchone()
    if turn is not None and turn[0] == 1:
        return totals(db, turn_id)
    # Older research turns already had this journal. Keep their original receipt
    # semantics; older ordinary turns cannot claim lifetime totals after a resume.
    snapshot = db.execute(
        "SELECT snapshot FROM turn_settings WHERE turn_id=?", (turn_id,)
    ).fetchone()
    if snapshot and json.loads(snapshot[0]).get("researchMode") in ("web", "deep"):
        return totals(db, turn_id)
    return dict.fromkeys(FIELDS)


def account(db, turn_id, fields):
    if db.execute(
        "SELECT 1 FROM research_model_calls WHERE turn_id=? AND lane='foreground'", (turn_id,)
    ).fetchone():
        # This includes steered/discarded completions already. Never add their
        # separate historical receipt a second time.
        return {**fields, **_accounted(db, turn_id)}
    return fields


def _refresh(db, turn_id):
    values = _accounted(db, turn_id)
    db.execute(
        "UPDATE turns SET " + ",".join(key + "=?" for key in FIELDS) + " WHERE id=?",
        (*values.values(), turn_id),
    )


def _value(completion, key):
    return _counter(getattr(completion, key, None), key)


def _counter(value, key):
    limit = MAX_TOKEN_COUNT if key.endswith("_tokens") else MAX_SAFE_NUMBER
    if type(value) not in (int, float) or not 0 <= value <= limit or not math.isfinite(value):
        return None
    return float(value) if key == "cost_usd" else value if type(value) is int else None


def _label(value, limit):
    return value if isinstance(value, str) and 1 <= len(value) <= limit else None


def _failure(exc):
    from pi.turns import inference_control

    token = inference_control.current()
    reason = token.reason if token is not None else None
    if reason == "deadline":
        return "timeout"
    if reason == "preempted":
        return "preempted"
    if reason == "shutdown":
        return "interrupted"
    if reason is not None or isinstance(exc, inference_control.InferenceCancelled):
        return "cancelled"
    error_reason = getattr(exc, "reason", None)
    if isinstance(error_reason, str) and error_reason in {
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "PoolTimeout",
    }:
        return "timeout"
    return "provider_error"


def observer(store, execution, role="answer"):
    """Observe an actual invocation without replacing any provider capabilities."""
    if not execution or not execution.get("turnExecutionId"):
        return None
    turn_id = execution["turnExecutionId"]
    lane = "thinking" if role == "thinking" else "foreground"

    def observe(invoke, *, provider, model, role, provider_id=None, model_id=None):
        from pi.models.providers import ProviderUnavailable

        if role not in ROLES:
            raise ProviderUnavailable("Unknown inference role.")
        identity = uuid.uuid4().hex
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = db.execute("SELECT session_id FROM turns WHERE id=?", (turn_id,)).fetchone()
            if (
                turn is None
                or db.execute(
                    "SELECT 1 FROM forgotten_sessions WHERE session_id=?", (turn[0],)
                ).fetchone()
            ):
                raise ProviderUnavailable("Inference turn unavailable.")
            db.execute(
                "INSERT INTO research_model_calls"
                "(id,turn_id,provider,model,role,status,started_at,lane,provider_id,model_id) "
                "VALUES(?,?,?,?,?,'pending',?,?,?,?)",
                (
                    identity,
                    turn_id,
                    _label(provider, 100) or "unknown",
                    _label(model, 300) or "unknown",
                    role,
                    time.time(),
                    lane,
                    _label(provider_id, 100),
                    _label(model_id, 200),
                ),
            )
            if lane == "foreground":
                _refresh(db, turn_id)
            db.commit()
        started = time.monotonic()
        try:
            from pi.turns import inference_control

            inference_control.check()
            completion = invoke()
        except Exception as exc:
            with store._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE research_model_calls SET status='failed',ended_at=?,elapsed_ms=?,"
                    "failure_kind=? WHERE id=? AND status='pending'",
                    (
                        time.time(),
                        max(0, round((time.monotonic() - started) * 1000)),
                        _failure(exc),
                        identity,
                    ),
                )
                if lane == "foreground":
                    _refresh(db, turn_id)
                db.commit()
            raise
        elapsed = max(0, round((time.monotonic() - started) * 1000))
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE research_model_calls SET status='reported',ended_at=?,elapsed_ms=?,"
                "actual_provider=?,actual_model=?,"
                + ",".join(key + "=?" for key in FIELDS)
                + " WHERE id=? AND status='pending'",
                (
                    time.time(),
                    elapsed,
                    _label(completion.provider, 100),
                    _label(completion.model, 300),
                    *(_value(completion, key) for key in FIELDS),
                    identity,
                ),
            )
            if lane == "foreground":
                _refresh(db, turn_id)
            db.commit()
        return completion

    return observe


def receipt(store, turn_id):
    """Bounded metadata projection; aggregate time is call time, never wall time."""
    with store._connect() as db:
        db.execute("BEGIN")
        turn = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        if (
            turn is None
            or db.execute(
                "SELECT 1 FROM forgotten_sessions WHERE session_id=?", (turn["session_id"],)
            ).fetchone()
        ):
            raise ValueError("Turn usage unavailable.")
        covered = turn["usage_version"] == 1
        rows = db.execute(
            "SELECT * FROM research_model_calls WHERE turn_id=? "
            "ORDER BY started_at DESC,id DESC LIMIT ?",
            (turn_id, MAX_ATTEMPTS + 1),
        ).fetchall()
        return {
            "schemaVersion": 1,
            "turn_id": turn_id,
            "session_id": turn["session_id"],
            "content_status": "available",
            "scope": "turn_provider_attempts",
            "coverage": "complete" if covered else "legacy_partial",
            "foreground": _summary(db, turn_id, "foreground", covered),
            "thinking": _summary(db, turn_id, "thinking", covered),
            "attempts": [
                {
                    key: _counter(row[key], key) if key in (*FIELDS, "elapsed_ms") else row[key]
                    for key in (
                        "id",
                        "lane",
                        "role",
                        "provider_id",
                        "model_id",
                        "provider",
                        "model",
                        "actual_provider",
                        "actual_model",
                        "status",
                        "failure_kind",
                        "started_at",
                        "ended_at",
                        "elapsed_ms",
                        *FIELDS,
                    )
                }
                | {
                    "provider": _label(row["provider"], 100) or "unknown",
                    "model": _label(row["model"], 300) or "unknown",
                    "provider_id": _label(row["provider_id"], 100),
                    "model_id": _label(row["model_id"], 200),
                    "actual_provider": _label(row["actual_provider"], 100),
                    "actual_model": _label(row["actual_model"], 300),
                }
                for row in reversed(rows[:MAX_ATTEMPTS])
            ],
            "attempts_truncated": len(rows) > MAX_ATTEMPTS,
        }


def wrap(store, execution, provider, role="answer"):
    observe = observer(store, execution, role)
    if observe is None:
        return provider
    return Metered(observe, provider, role)


class Metered:
    def __init__(self, observe, provider, role):
        self.observe, self.provider, self.role = observe, provider, role

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def complete(self, messages, *, model):
        return self._call(lambda: self.provider.complete(messages, model=model), model)

    def complete_bounded(self, messages, *, model, timeout):
        from pi.models.providers import ProviderUnavailable

        bounded = getattr(self.provider, "complete_bounded", None)
        if not callable(bounded):
            raise ProviderUnavailable("Research provider does not support the configured timeout.")
        return self._call(lambda: bounded(messages, model=model, timeout=timeout), model)

    def _call(self, invoke, model):
        return self.observe(invoke, provider=self.name, model=model, role=self.role)
