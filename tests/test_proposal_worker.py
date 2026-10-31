"""The proposal timer: what a tick runs, and that a failure keeps conversation text out of logs.

The worker's error handler is a privacy promise: an exception's text may quote what the
owner said, so the log may carry only the fixed sentence. These tests pin both halves:
a tick calls the proposals engine in order, and a pass that raises leaves no trace of
the exception in the logging output.
"""

import logging

import pytest

from pi.schedule import proposal_worker, proposals


class Recording:
    """A stand-in for a proposals function: records every call, then returns or raises."""

    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def engine(monkeypatch):
    """Fakes for the three proposals functions the worker drives, each recording its calls."""
    fakes = Recording(True), Recording((False, "complete")), Recording("pass finished")
    for name, fake in zip(("due", "model_call", "run_pass"), fakes, strict=True):
        monkeypatch.setattr(proposals, name, fake)
    return fakes


def worker(store, **kwargs):
    return proposal_worker.ProposalWorker(store, "router", **kwargs)


@pytest.mark.parametrize(
    "interval_hours,check_seconds",
    [(0, 1.0), (-1.0, 1.0), (1.0, 0.0), (1.0, -0.5)],
)
def test_rejects_non_positive_intervals(interval_hours, check_seconds):
    with pytest.raises(ValueError):
        worker(object(), interval_hours=interval_hours, check_seconds=check_seconds)


def test_not_due_returns_none_and_runs_nothing(engine):
    due, model_call, run_pass = engine
    due.result = False
    store = object()
    timer = worker(store)
    assert timer.tick() is None
    assert due.calls == [((store, timer.interval_seconds), {})]
    assert model_call.calls == []
    assert run_pass.calls == []


def test_due_calls_the_pass_with_the_bound_completion(engine):
    _, model_call, run_pass = engine
    store, complete = object(), object()
    model_call.result = (True, complete)
    run_pass.result = {"passId": "pps_1", "state": "completed"}
    timer = worker(store)
    assert timer.tick() == {"passId": "pps_1", "state": "completed"}
    assert model_call.calls == [((store, "router"), {})]
    assert run_pass.calls == [((store, complete), {"role_enabled": True})]


def test_failed_pass_leaves_no_exception_text_in_the_log(monkeypatch, caplog):
    store = object()
    timer = worker(store)
    calls = []

    def due(*args):
        calls.append(("due", args))
        return True

    def fail(*args):
        calls.append(("model_call", args))
        timer._stop.set()  # one failing tick is enough; end the loop so _run returns
        raise RuntimeError("secret conversation text")

    def run_pass(*args, **kwargs):
        calls.append(("run_pass", args, kwargs))
        return {}

    monkeypatch.setattr(proposals, "due", due)
    monkeypatch.setattr(proposals, "model_call", fail)
    monkeypatch.setattr(proposals, "run_pass", run_pass)
    with caplog.at_level(logging.ERROR):
        timer._run()
    assert [record.getMessage() for record in caplog.records] == [
        "Proposal pass failed; see the latest pass record."
    ]
    assert "secret conversation text" not in caplog.text
    assert all(not record.exc_info for record in caplog.records)
    assert [call[0] for call in calls] == ["due", "model_call"]


def test_start_twice_raises(engine):
    timer = worker(object(), check_seconds=0.01)
    timer.start()
    try:
        with pytest.raises(RuntimeError):
            timer.start()
    finally:
        timer.close()


def test_close_stops_and_joins_the_thread(engine):
    timer = worker(object(), check_seconds=0.01)
    timer.start()
    thread = timer._thread
    timer.close()
    assert not thread.is_alive()


def test_close_before_start_is_harmless():
    worker(object()).close()
