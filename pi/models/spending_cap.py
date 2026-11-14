"""One daily cap over every paid model call Pi makes, with a meter and a stop (R-M6).

Every call through a paid adapter (OpenRouter models that are not free, and the direct-key
providers) is admitted here first and written down after. The owner's cap (`spendingCap` in
owner preferences) has two limits for his local day: money, from the cost the provider reports,
and tokens, which every paid call counts (direct keys report no price, and a price is never
guessed). Once either is reached, the next paid call is refused before it is sent; the refusal
is a model refusal, so a role moves on to its fallback (a free or local model) instead of
failing. A call holds its input's tokens (estimated from its length) from admission, and one
whose input does not fit in what is left of the token limit is refused, so calls in flight
together cannot all pass under it. Output and price are counted when a call ends, so a day can
end over the token limit by the answers that were in flight, and over the money limit by the
whole price of the calls in flight (a price is known only from the provider's report). A call
that fails after it was sent keeps the input hold (it may have been billed); one the provider
refused as a model refusal, or one that could not be billed (NotBilled: never sent, or turned
away for its key, credit or quota), counts nothing. If the ledger itself cannot be read, paid
calls are refused (fail closed) the same way; a ledger write that fails after a call ended is
logged and never replaces the call's answer or error.

Subscriptions (ChatGPT, Claude) are flat-rate and local models are free: they are not counted.
The ledger holds provider and model ids, counts and states, never a prompt or an answer.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import UTC, datetime

from pi.models import inference_control
from pi.models.openrouter import ModelUnusable, PaidModelRefused
from pi.models.providers import NotBilled
from pi.owner import preferences as prefs

log = logging.getLogger(__name__)

KEEP_DAYS = 40
CHARS_PER_TOKEN = 4
SCHEMA = """
CREATE TABLE IF NOT EXISTS paid_model_calls (
 id TEXT PRIMARY KEY, day TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('running','done','failed')),
 cost_usd REAL, tokens INTEGER NOT NULL DEFAULT 0,
 tokens_estimated INTEGER NOT NULL DEFAULT 0 CHECK(tokens_estimated IN (0,1)),
 started_at REAL NOT NULL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS paid_model_calls_by_day ON paid_model_calls(day);
"""
REACHED = (
    "Today's spending cap is reached, so paid models are paused until tomorrow. "
    "Raise it with `conker spending cap`."
)


class CapReached(PaidModelRefused):
    """The owner's daily cap is reached: no paid call is sent until his next local day."""


class CapUnavailable(PaidModelRefused):
    """The ledger could not be read or written, so no paid call is sent (fail closed)."""


UNAVAILABLE = "Pi could not check today's spending cap, so paid models are paused for now."


def _preferences(db) -> prefs.OwnerPreferences:
    row = db.execute("SELECT preferences FROM owner_preferences WHERE singleton=1").fetchone()
    return prefs.OwnerPreferences.model_validate_json(row["preferences"])


def is_enabled(db) -> bool:
    """Whether the owner's daily cap is switched on; an unreadable setting raises (callers refuse)."""
    return _preferences(db).spendingCap.enabled


def _used(db, day: str) -> dict:
    row = db.execute(
        "SELECT COALESCE(SUM(cost_usd),0) AS cost, COALESCE(SUM(tokens),0) AS tokens, "
        "COUNT(*) AS calls, COALESCE(SUM(state='running'),0) AS running, "
        "COALESCE(SUM(state='done' AND cost_usd IS NULL),0) AS unpriced, "
        "COALESCE(SUM(tokens_estimated),0) AS estimated, "
        "COALESCE(SUM(state='failed'),0) AS failed "
        "FROM paid_model_calls WHERE day=?",
        (day,),
    ).fetchone()
    return {
        "costUsd": round(float(row["cost"]), 6),
        "tokens": int(row["tokens"]),
        "calls": int(row["calls"]),
        "running": int(row["running"]),
        "unpricedCalls": int(row["unpriced"]),
        "estimatedTokenCalls": int(row["estimated"]),
        "failedCalls": int(row["failed"]),
    }


def _reached(cap: prefs.SpendingCap, used: dict) -> list[str]:
    if not cap.enabled:
        return []
    reasons = []
    if used["costUsd"] * 100 >= cap.cents:
        reasons.append("money")
    if used["tokens"] >= cap.tokens:
        reasons.append("tokens")
    return reasons


def status(store, *, now: datetime | None = None) -> dict:
    """The meter for the owner's local day: what paid calls used, the cap, and whether it stops."""
    now = now or datetime.now(UTC)
    with store._connect() as db:
        db.execute("BEGIN")
        preferences = _preferences(db)
        day = prefs.local_day(preferences, now)
        used = _used(db, day)
    cap = preferences.spendingCap
    return {
        "day": day,
        "timeZone": preferences.quietHours.timeZone,
        "cap": cap.model_dump(),
        "used": used,
        "reached": _reached(cap, used),
    }


def admit(store, provider: str, model: str, messages=(), *, now: datetime | None = None) -> str:
    """Write down a paid call before it is sent, holding its input's tokens, or refuse it when
    today's cap is reached. Any failure to check is a refusal too (CapUnavailable)."""
    try:
        return _admit(store, provider, model, _estimate(messages, ""), now or datetime.now(UTC))
    except CapReached:
        raise
    except Exception as exc:
        raise CapUnavailable(UNAVAILABLE) from exc


def _admit(store, provider: str, model: str, held: int, now: datetime) -> str:
    identity = "pmc_" + secrets.token_hex(12)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            preferences = _preferences(db)
            day = prefs.local_day(preferences, now)
            cap, used = preferences.spendingCap, _used(db, day)
            if _reached(cap, used) or (cap.enabled and used["tokens"] + held > cap.tokens):
                raise CapReached(REACHED)
            db.execute(
                "INSERT INTO paid_model_calls(id, day, provider, model, state, tokens, "
                "tokens_estimated, started_at) VALUES (?,?,?,?,'running',?,1,?)",
                (identity, day, provider, model[:200], held, now.timestamp()),
            )
            db.execute(
                "DELETE FROM paid_model_calls WHERE started_at<? AND state!='running'",
                (now.timestamp() - KEEP_DAYS * 86400,),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return identity


def _estimate(messages, text: str) -> int:
    chars = sum(len(getattr(m, "content", "") or "") for m in messages) + len(text or "")
    return max(1, chars // CHARS_PER_TOKEN)


def settle(store, identity: str, completion=None, messages=(), *, refused: bool = False) -> None:
    """Count a finished call. A failed one (completion None) keeps its input hold, since it may
    have been billed; a refused one (a model refusal or NotBilled) counts nothing."""
    if completion is None and refused:
        values = ("failed", None, 0, 0)
    elif completion is None:
        values = ("failed", None, _estimate(messages, ""), 1)
    else:
        reported = [completion.input_tokens, completion.output_tokens]
        known = all(isinstance(value, int) and value >= 0 for value in reported)
        tokens = sum(reported) if known else _estimate(messages, completion.text)
        cost = completion.cost_usd
        if not isinstance(cost, int | float) or cost < 0:
            cost = None
        values = ("done", cost, tokens, 0 if known else 1)
    try:
        _write(store, identity, values)
    except Exception as exc:
        # The row stays running (its input hold counts) until recover_interrupted at the next
        # start; the call's own answer or error is what the caller gets.
        log.warning("spending ledger: could not settle %s (%s)", identity, type(exc).__name__)


def _write(store, identity: str, values: tuple) -> None:
    with store._connect() as db:
        db.execute(
            "UPDATE paid_model_calls SET state=?, cost_usd=?, tokens=?, tokens_estimated=?, "
            "finished_at=? WHERE id=? AND state='running'",
            (*values, time.time(), identity),
        )


def recover_interrupted(store) -> int:
    """At startup: a call that was running when Pi stopped did not report; mark it failed."""
    with store._connect() as db:
        # The input hold stays: the call may have been billed.
        return db.execute(
            "UPDATE paid_model_calls SET state='failed', finished_at=? WHERE state='running'",
            (time.time(),),
        ).rowcount


class Capped:
    """A paid adapter behind the daily cap; everything else reads through to the adapter.

    `paid(model)` says whether a call would be sent and cost money; a call that is not paid
    (a free model, or one the adapter refuses itself) is neither checked nor counted."""

    def __init__(self, adapter, store, paid) -> None:
        self._adapter = adapter
        self._store = store
        self._paid = paid

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)  # never recurse before __init__ ran (copy, pickle)
        return getattr(self._adapter, name)

    def complete(self, messages, *, model: str):
        return self.complete_bounded(messages, model=model, timeout=self._adapter.timeout)

    def complete_bounded(self, messages, *, model: str, timeout: float):
        if not self._paid(model):
            return self._adapter.complete_bounded(messages, model=model, timeout=timeout)
        inference_control.check()  # a call stopped before it is sent holds nothing
        identity = admit(self._store, self._adapter.name, model, messages)
        try:
            completion = self._adapter.complete_bounded(messages, model=model, timeout=timeout)
        except (ModelUnusable, NotBilled):
            settle(self._store, identity, refused=True)
            raise
        except BaseException:
            settle(self._store, identity, messages=messages)
            raise
        settle(self._store, identity, completion, messages)
        return completion


def openrouter_paid(adapter):
    """OpenRouter: with paid models on, a model not listed as free is paid (unknown = paid)."""

    def paid(model: str) -> bool:
        if not adapter.allow_paid:
            return False  # the adapter refuses every paid model itself
        # Without the catalogue the adapter refuses the call before sending it, so its error
        # (or an owner stop) goes to the caller here, before anything is admitted.
        info = adapter.catalogue().get(model)
        # A model missing from the catalogue is refused by the adapter before anything is sent.
        return info is not None and not info.is_free

    return paid


def direct_paid(adapter):
    """A direct-key provider: every call it sends is paid; without key or opt-in it sends none."""
    return lambda model: bool(adapter.api_key and adapter.allow_paid)


def wrap(store, hosted, providers: dict, direct_names) -> tuple:
    """Put OpenRouter and the direct-key providers behind the cap; others stay as they are."""
    capped_hosted = Capped(hosted, store, openrouter_paid(hosted)) if hosted else None
    names = set(direct_names)
    capped = {
        name: Capped(adapter, store, direct_paid(adapter)) if name in names else adapter
        for name, adapter in providers.items()
    }
    return capped_hosted, capped
