"""Content-free provider attempt receipts for research turns."""
import math
import time
import uuid

FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "cost_usd")
SCHEMA = """
CREATE TABLE IF NOT EXISTS research_model_calls (
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
 provider TEXT NOT NULL, model TEXT NOT NULL, role TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','reported','failed')),
 started_at REAL NOT NULL, ended_at REAL,
 input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER, cost_usd REAL
);
CREATE INDEX IF NOT EXISTS research_calls_turn ON research_model_calls(turn_id);
"""


def totals(db, turn_id):
    rows = db.execute("SELECT * FROM research_model_calls WHERE turn_id=?", (turn_id,)).fetchall()
    return {key: sum(row[key] for row in rows) if rows and all(
        row['status'] == 'reported' and row[key] is not None for row in rows) else None for key in FIELDS}


def account(db, turn_id, fields):
    if db.execute("SELECT 1 FROM research_model_calls WHERE turn_id=?", (turn_id,)).fetchone():
        return {**fields, **totals(db, turn_id)}
    return fields


def _refresh(db, turn_id):
    values = totals(db, turn_id)
    db.execute("UPDATE turns SET " + ",".join(key + "=?" for key in FIELDS) + " WHERE id=?",
               (*values.values(), turn_id))


def _value(completion, key):
    value = getattr(completion, key, None)
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return value if key == "cost_usd" or type(value) is int else None


def wrap(store, execution, provider, role="answer"):
    if not execution or execution.get("researchMode") not in ("web", "deep") or not execution.get("turnExecutionId"):
        return provider
    return Metered(store, execution["turnExecutionId"], provider, role)


class Metered:
    def __init__(self, store, turn_id, provider, role):
        self.store, self.turn_id, self.provider, self.role = store, turn_id, provider, role
        self.name = provider.name
        self.supports_images = getattr(provider, "supports_images", False)

    def health(self):
        return self.provider.health()

    def complete(self, messages, *, model):
        return self._call(lambda: self.provider.complete(messages, model=model), model)

    def complete_bounded(self, messages, *, model, timeout):
        from .providers import ProviderUnavailable

        bounded = getattr(self.provider, "complete_bounded", None)
        if not callable(bounded):
            raise ProviderUnavailable("Research provider does not support the configured timeout.")
        return self._call(lambda: bounded(messages, model=model, timeout=timeout), model)

    def _call(self, invoke, model):
        identity = uuid.uuid4().hex
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO research_model_calls(id,turn_id,provider,model,role,status,started_at) "
                       "VALUES(?,?,?,?,?,'pending',?)", (identity, self.turn_id, self.name, model, self.role, time.time()))
            _refresh(db, self.turn_id)
            db.commit()
        try:
            completion = invoke()
        except Exception:
            with self.store._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE research_model_calls SET status='failed',ended_at=? WHERE id=?", (time.time(), identity))
                _refresh(db, self.turn_id)
                db.commit()
            raise
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE research_model_calls SET status='reported',ended_at=?," +
                       ",".join(key + "=?" for key in FIELDS) + " WHERE id=?",
                       (time.time(), *(_value(completion, key) for key in FIELDS), identity))
            _refresh(db, self.turn_id)
            db.commit()
        return completion
