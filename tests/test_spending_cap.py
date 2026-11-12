"""One daily cap over every paid model call (pi.models.spending_cap, R-M6)."""

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_service_inference import OWNER, body, client, digest, save_role
from test_service_inference import Router as FakeRouter

from pi.api import browser_contract
from pi.api import server as api
from pi.conversation.store import Store
from pi.models import spending_cap
from pi.models.openrouter import ModelUnusable, PaidModelRefused
from pi.models.providers import Completion, Message, ProviderUnavailable
from pi.owner import preferences as prefs

NOON = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)  # 12:00 in the default Asia/Jerusalem


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def set_cap(store, **cap):
    current = prefs.load(store)
    document = current["preferences"]
    document["spendingCap"] = {**document["spendingCap"], **cap}
    prefs.save(
        store,
        prefs.UpdatePreferences(expected_revision=current["revision"], preferences=document),
    )


class Paid:
    """A paid adapter that answers with reported tokens and, optionally, a price."""

    name, timeout, allow_paid, api_key = "openai", 30.0, True, "synthetic-key"

    def __init__(self, tokens=(100, 50), cost=None, fail=False):
        self.tokens, self.cost, self.fail = tokens, cost, fail
        self.calls = []

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append((model, timeout))
        if self.fail:
            raise ProviderUnavailable("offline")
        return Completion("answer", model, self.name, *self.tokens, cost_usd=self.cost)


def ask(adapter, model="gpt-paid"):
    return adapter.complete([Message(role="user", content="x" * 400)], model=model)


def test_paid_calls_are_counted_and_the_meter_shows_them(store):
    capped = spending_cap.Capped(Paid(cost=0.0125), store, lambda model: True)
    ask(capped)
    ask(capped)
    meter = spending_cap.status(store)
    assert meter["used"] == {
        "costUsd": 0.025,
        "tokens": 300,
        "calls": 2,
        "running": 0,
        "unpricedCalls": 0,
        "estimatedTokenCalls": 0,
        "failedCalls": 0,
    }
    assert meter["cap"] == prefs.DEFAULT_SPENDING_CAP and meter["reached"] == []


def test_the_token_limit_stops_the_next_paid_call_before_it_is_sent(store):
    set_cap(store, tokens=200)
    paid = Paid()  # direct keys report tokens but no price
    capped = spending_cap.Capped(paid, store, lambda model: True)
    ask(capped)  # 150 tokens
    ask(capped)  # 300: over, but it was admitted while under
    with pytest.raises(spending_cap.CapReached) as stopped:
        ask(capped)
    assert len(paid.calls) == 2
    assert str(stopped.value) == spending_cap.REACHED
    # A model refusal, so a role moves on to its fallback instead of failing the turn.
    assert isinstance(stopped.value, PaidModelRefused | ModelUnusable)
    meter = spending_cap.status(store)
    assert meter["reached"] == ["tokens"] and meter["used"]["unpricedCalls"] == 2


def test_the_money_limit_counts_reported_cost(store):
    set_cap(store, cents=2)
    capped = spending_cap.Capped(Paid(cost=0.02), store, lambda model: True)
    ask(capped)
    with pytest.raises(spending_cap.CapReached):
        ask(capped)
    assert spending_cap.status(store)["reached"] == ["money"]


def test_a_cap_switched_off_still_counts_but_never_stops(store):
    set_cap(store, enabled=False, tokens=0, cents=0)
    capped = spending_cap.Capped(Paid(), store, lambda model: True)
    ask(capped)
    ask(capped)
    meter = spending_cap.status(store)
    assert meter["used"]["calls"] == 2 and meter["reached"] == []


def test_the_day_is_the_owners_local_day(store):
    set_cap(store, tokens=100)
    yesterday = NOON - timedelta(days=1)
    identity = spending_cap.admit(store, "openai", "gpt-paid", now=yesterday)
    spending_cap.settle(store, identity, Completion("a", "m", "openai", 500, 500))
    spending_cap.admit(store, "openai", "gpt-paid", now=NOON)  # yesterday's use is not today's
    assert spending_cap.status(store, now=yesterday)["reached"] == ["tokens"]
    assert spending_cap.status(store, now=NOON)["used"]["tokens"] == 1  # today's hold only


def test_a_call_that_reports_no_tokens_is_estimated_and_a_failed_one_keeps_its_input(store):
    spending_cap.Capped(Paid(tokens=(None, None)), store, lambda m: True).complete(
        [Message(role="user", content="y" * 400)], model="m"
    )
    with pytest.raises(ProviderUnavailable):
        ask(spending_cap.Capped(Paid(fail=True), store, lambda model: True))
    used = spending_cap.status(store)["used"]
    answered = (400 + len("answer")) // spending_cap.CHARS_PER_TOKEN
    assert used["tokens"] == answered + 400 // spending_cap.CHARS_PER_TOKEN  # it may be billed
    assert (used["estimatedTokenCalls"], used["failedCalls"], used["calls"]) == (2, 1, 2)


class Refusing(Paid):
    def complete_bounded(self, messages, *, model, timeout):
        raise ModelUnusable("gpt-paid: HTTP 404")


def test_a_model_refusal_counts_nothing(store):
    with pytest.raises(ModelUnusable):
        ask(spending_cap.Capped(Refusing(), store, lambda model: True))
    used = spending_cap.status(store)["used"]
    assert (used["tokens"], used["failedCalls"]) == (0, 1)


def test_calls_in_flight_hold_their_input_so_they_cannot_all_pass(store):
    set_cap(store, tokens=250)
    long = [Message(role="user", content="z" * 800)]  # 200 tokens held each
    spending_cap.admit(store, "openai", "gpt-paid", long)
    spending_cap.admit(store, "openai", "gpt-paid", long)  # 200 < 250: admitted
    with pytest.raises(spending_cap.CapReached):
        spending_cap.admit(store, "openai", "gpt-paid", long)  # 400 held: stopped


def test_a_ledger_that_cannot_be_read_pauses_paid_calls_as_a_model_refusal(store):
    with store._connect() as db:
        db.execute("UPDATE owner_preferences SET preferences='not json'")
    paid = Paid()
    with pytest.raises(spending_cap.CapUnavailable) as paused:
        ask(spending_cap.Capped(paid, store, lambda model: True))
    assert isinstance(paused.value, ModelUnusable) and paid.calls == []


def test_the_wrapper_does_not_recurse_before_it_is_built():
    bare = spending_cap.Capped.__new__(spending_cap.Capped)
    with pytest.raises(AttributeError):
        bare._adapter  # noqa: B018


def test_a_call_that_is_not_paid_is_neither_checked_nor_counted(store):
    set_cap(store, tokens=0)
    paid = Paid()
    ask(spending_cap.Capped(paid, store, lambda model: False))
    assert len(paid.calls) == 1 and spending_cap.status(store)["used"]["calls"] == 0


def test_a_call_running_when_pi_stopped_is_marked_failed_at_the_next_start(store):
    spending_cap.admit(store, "openai", "gpt-paid")
    assert spending_cap.status(store)["used"]["running"] == 1
    assert spending_cap.recover_interrupted(store) == 1
    assert spending_cap.status(store)["used"]["failedCalls"] == 1


def test_the_ledger_holds_ids_and_counts_only(store):
    ask(spending_cap.Capped(Paid(), store, lambda model: True))
    with store._connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM paid_model_calls")]
    assert "x" * 40 not in json.dumps(rows) and "answer" not in json.dumps(rows)


class OpenRouter:
    name, timeout = "openrouter", 30.0

    def __init__(self, allow_paid=True):
        self.allow_paid = allow_paid

    def catalogue(self):
        return {
            "free-model": SimpleNamespace(is_free=True),
            "paid-model": SimpleNamespace(is_free=False),
        }


def test_only_paid_openrouter_models_and_direct_keys_are_capped():
    paid = spending_cap.openrouter_paid(OpenRouter())
    # An unlisted model is refused by the adapter itself before anything is sent.
    assert not paid("free-model") and paid("paid-model") and not paid("unlisted")
    assert not spending_cap.openrouter_paid(OpenRouter(allow_paid=False))("paid-model")
    subscription, direct = object(), Paid()
    hosted, providers = spending_cap.wrap(
        None, OpenRouter(), {"claude": subscription, "openai": direct}, ["openai", "groq"]
    )
    assert isinstance(hosted, spending_cap.Capped) and hosted.name == "openrouter"
    assert providers["claude"] is subscription
    assert isinstance(providers["openai"], spending_cap.Capped)
    assert providers["openai"].api_key == "synthetic-key"  # everything else reads through
    assert spending_cap.wrap(None, None, {}, [])[0] is None


def test_preferences_saved_before_the_cap_read_the_default(store):
    with store._connect() as db:
        row = db.execute("SELECT preferences FROM owner_preferences").fetchone()
        stored = json.loads(row[0])
        stored.pop("spendingCap")
        db.execute("UPDATE owner_preferences SET preferences=?", (json.dumps(stored),))
    assert prefs.load(store)["preferences"]["spendingCap"] == prefs.DEFAULT_SPENDING_CAP
    assert spending_cap.status(store)["cap"] == prefs.DEFAULT_SPENDING_CAP


def test_a_role_on_a_capped_model_sends_nothing_once_the_cap_is_reached(monkeypatch, store):
    set_cap(store, tokens=0)
    save_role(store, "memory-sorting", fallback="backup")
    inner = Paid()
    inner.name = "hosted"
    router = FakeRouter(others=[spending_cap.Capped(inner, store, lambda model: True)])
    answer = client(monkeypatch, store, router).post(
        "/inference", json=body(), headers={"X-Pi-Service-Key": "memorygate-key-synthetic"}
    )
    assert answer.status_code == 503, answer.text
    assert inner.calls == []  # neither the model nor its fallback was sent


def test_the_meter_is_an_owner_read(monkeypatch, store):
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin-key", raising=False)
    monkeypatch.setattr(api.app.state, "owner_key_hash", digest(OWNER), raising=False)
    http = TestClient(api.app)
    assert http.get("/inference/spending").status_code == 401
    meter = http.get("/inference/spending", headers={"X-Pi-Owner-Key": OWNER})
    assert meter.status_code == 200 and meter.json()["cap"] == prefs.DEFAULT_SPENDING_CAP
    assert browser_contract.owner_allowed("GET", "/inference/spending")
    assert not browser_contract.runtime_allowed("GET", "/inference/spending")
    # Raising the cap is a preferences save, which asks for the password.
    assert browser_contract.password_write("POST", "/owner/preferences")
