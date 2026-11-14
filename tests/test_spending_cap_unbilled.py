"""The daily cap counts only what a provider can bill (hunt 2, spend-F1..F3)."""

from contextlib import closing

import httpx
import pytest
from test_spending_cap import Paid, set_cap

from pi.conversation.store import Store
from pi.models import direct, openrouter, spending_cap
from pi.models.openrouter import ModelInfo, ModelUnusable, OpenRouterProvider
from pi.models.providers import Message, ProviderUnavailable
from pi.turns import inference_control

LONG = [Message(role="user", content="q" * 4000)]  # 1,000 tokens held if admitted


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def used(store):
    return spending_cap.status(store)["used"]


def never_sent(*args, **kwargs):
    raise AssertionError("nothing may be sent")


def paid_model():
    pricing = {"prompt": "0.001", "completion": "0.002", "request": "0"}
    return ModelInfo("paid/model", 8000, 0.001, 0.002, True, pricing)


class CatalogueDown(OpenRouterProvider):
    def catalogue(self, *, force=False):
        inference_control.check()
        raise ProviderUnavailable("catalogue unavailable: ConnectError")


class Listed(OpenRouterProvider):
    def catalogue(self, *, force=False):
        inference_control.check()
        return {"paid/model": paid_model()}


def capped_openrouter(store, cls):
    adapter = cls("synthetic-key", allow_paid=True)
    return spending_cap.Capped(adapter, store, spending_cap.openrouter_paid(adapter))


def test_a_catalogue_outage_holds_nothing_for_calls_never_sent(monkeypatch, store):
    monkeypatch.setattr(openrouter.httpx, "post", never_sent)
    capped = capped_openrouter(store, CatalogueDown)
    for model in ("some/free-model:free", "paid/model"):
        with pytest.raises(ProviderUnavailable):
            capped.complete(LONG, model=model)
    assert used(store)["calls"] == 0 and used(store)["tokens"] == 0


def test_a_call_cancelled_before_it_is_sent_holds_nothing(monkeypatch, store):
    monkeypatch.setattr(openrouter.httpx, "post", never_sent)
    for capped in (
        capped_openrouter(store, Listed),
        spending_cap.Capped(Paid(), store, lambda model: True),
    ):
        token = inference_control.Token()
        with inference_control.bind(token), pytest.raises(inference_control.InferenceCancelled):
            token.cancel()  # the owner stops it before the adapter sends anything
            capped.complete(LONG, model="paid/model")
    assert used(store)["tokens"] == 0


def answer(status):
    request = httpx.Request("POST", "https://provider.invalid/v1")
    return lambda *args, **kwargs: httpx.Response(status, request=request, json={})


@pytest.mark.parametrize("status", [401, 402, 429])
def test_a_key_credit_or_quota_refusal_is_not_billed(monkeypatch, store, status):
    monkeypatch.setattr(openrouter.httpx, "post", answer(status))
    monkeypatch.setattr(direct.httpx, "post", answer(status))
    key = direct.OpenAIProvider("synthetic-key", allow_paid=True)
    for capped in (
        capped_openrouter(store, Listed),
        spending_cap.Capped(key, store, spending_cap.direct_paid(key)),
    ):
        with pytest.raises(ProviderUnavailable) as refused:
            capped.complete(LONG, model="paid/model")
        assert refused.value.reason == "HTTPStatusError"  # the reason is unchanged
    assert used(store)["tokens"] == 0 and used(store)["failedCalls"] == 2


def test_a_billable_failure_still_keeps_its_input_hold(monkeypatch, store):
    monkeypatch.setattr(direct.httpx, "post", answer(500))
    key = direct.OpenAIProvider("synthetic-key", allow_paid=True)
    with pytest.raises(ProviderUnavailable):
        spending_cap.Capped(key, store, spending_cap.direct_paid(key)).complete(LONG, model="m")
    assert used(store)["tokens"] == 1000


def test_pis_own_refusal_before_sending_holds_nothing(monkeypatch, store):
    monkeypatch.setattr(direct.httpx, "post", never_sent)
    key = direct.AnthropicProvider("synthetic-key", allow_paid=True)
    capped = spending_cap.Capped(key, store, spending_cap.direct_paid(key))
    with pytest.raises(ProviderUnavailable):
        capped.complete([Message(role="system", content="s" * 4000)], model="claude-x")
    with pytest.raises(ProviderUnavailable):
        capped.complete(LONG, model="  ")
    assert used(store)["tokens"] == 0


class BreaksTheLedger(Paid):
    def __init__(self, store, error=None):
        super().__init__()
        self.store, self.error = store, error

    def complete_bounded(self, messages, *, model, timeout):
        def locked():
            raise RuntimeError("database is locked")

        self.store._connect = locked
        if self.error:
            raise self.error
        return super().complete_bounded(messages, model=model, timeout=timeout)


def test_a_ledger_write_after_a_billed_answer_never_loses_the_answer(store):
    # The provider answered (and billed); only the bookkeeping write after it fails.
    capped = spending_cap.Capped(BreaksTheLedger(store), store, lambda model: True)
    assert capped.complete(LONG, model="m").text == "answer"


def test_a_ledger_write_after_a_failure_keeps_the_original_error(store):
    stop = ModelUnusable("gpt-paid: HTTP 404")
    capped = spending_cap.Capped(BreaksTheLedger(store, stop), store, lambda model: True)
    with pytest.raises(ModelUnusable) as raised:
        capped.complete(LONG, model="m")
    assert raised.value is stop


def test_a_call_whose_input_is_over_the_headroom_is_refused(store):
    set_cap(store, tokens=1500)
    spending_cap.admit(store, "openai", "gpt-paid", LONG)  # 1,000 held, 500 left
    with pytest.raises(spending_cap.CapReached):
        spending_cap.admit(store, "openai", "gpt-paid", LONG)
    spending_cap.admit(store, "openai", "gpt-paid", [Message(role="user", content="q" * 2000)])
    assert used(store)["tokens"] == 1500  # exactly at the cap is still within it
