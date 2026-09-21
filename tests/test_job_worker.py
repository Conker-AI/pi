from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

from test_jobs import definition

from pi import jobs
from pi.job_worker import JobWorker
from pi.store import Store


def test_workers_share_durable_admission_and_restart_ready(tmp_path):
    with closing(Store(tmp_path / "worker.db")) as store:
        job = jobs.create(store, definition(), now=0)
        calls = []

        def invoke(target, **kwargs):
            calls.append(kwargs)
            return {"status": "completed"}

        first, second = JobWorker(store, invoke), JobWorker(store, invoke)
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda worker: worker.tick(now=3600), [first, second]))
        assert len(calls) == 1
        jobs.run_now(store, job["id"], "manual_ready_request", now=3601)
        second.tick(now=3602)
        assert len(calls) == 2
        assert all(row["status"] == "completed" for row in jobs.runs(store, job["id"]))


def test_worker_does_not_retry_uncertain_or_approval_runs(tmp_path):
    with closing(Store(tmp_path / "worker.db")) as store:
        jobs.create(store, definition(), now=0)
        calls = []

        def invoke(target, **kwargs):
            calls.append(kwargs)
            return {"status": "outcome_unknown"}

        worker = JobWorker(store, invoke)
        worker.tick(now=3600)
        worker.tick(now=7200)
        assert len(calls) == 1


def test_close_waits_for_inflight_dispatch_and_stops_next(tmp_path):
    with closing(Store(tmp_path / "worker.db")) as store:
        jobs.create(store, definition(), now=0)
        entered, release = Event(), Event()

        def invoke(target, **kwargs):
            entered.set()
            assert release.wait(3)
            return {"status": "completed"}

        worker = JobWorker(store, invoke, interval=60)
        worker.start()
        assert entered.wait(3)
        with ThreadPoolExecutor(1) as pool:
            closing_worker = pool.submit(worker.close)
            assert not closing_worker.done()
            release.set()
            closing_worker.result(timeout=3)
        assert not worker._thread.is_alive()
