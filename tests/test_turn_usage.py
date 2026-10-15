"""Every observed model invocation is counted once, with honest partial coverage."""

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_background_thinking import setup as setup_thinking
from test_background_thinking import wait_workers
from test_loop import loop_with
from test_model_roles import config
from test_research_usage import priced
from test_tool_turns import CALL, FakeGate, build
from test_turn_steering import SteerOnce

from pi.api import server as api
from pi.context import research_usage
from pi.conversation import forgetting
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.memory.ranking import ConfiguredMemoryRanker
from pi.models import roles as model_roles
from pi.models.providers import Completion, ProviderUnavailable
from pi.models.routing import Router
from pi.turns.loop import ActedWithoutReply, Loop


def test_ordinary_tool_steps_and_final_are_summed_once_after_request_replay(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, provider = build(store, [CALL, "Final answer"], FakeGate())
        priced(provider)
        sid = store.create_session()
        first = loop.run_turn(sid, "Use the tool", request_id="usage_request_0001")
        tid = first["turn_id"]
        before = research_usage.receipt(store, tid)
        assert before["coverage"] == "complete"
        assert before["foreground"]["attempt_count"] == 2
        assert before["foreground"]["totals"]["input_tokens"] == 20
        assert before["foreground"]["totals"]["cost_usd"] == pytest.approx(0.02)
        assert store.get_turn(tid)["output_tokens"] == 10
        replay = loop.run_turn(sid, "Use the tool", request_id="usage_request_0001")
        assert replay["replayed"] and research_usage.receipt(store, tid) == before
        assert len({row["id"] for row in before["attempts"]}) == 2
        assert len(provider.sent) == 2
        assert all(row["elapsed_ms"] >= 0 for row in before["attempts"])


def test_final_failure_then_resume_keeps_known_subtotal_and_unknown_attempt(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, provider = build(
            store, [CALL, ProviderUnavailable("private failure"), "Final"], FakeGate()
        )
        priced(provider)
        with pytest.raises(ActedWithoutReply) as failed:
            loop.run_turn(store.create_session(), "Use the tool")
        tid = failed.value.turn_id
        loop.resume_turn(tid)
        receipt = research_usage.receipt(store, tid)
        assert [row["status"] for row in receipt["attempts"]] == ["reported", "failed", "reported"]
        usage = receipt["foreground"]
        assert usage["known_subtotal"]["input_tokens"] == 20
        assert usage["unknown_counts"]["input_tokens"] == 1
        assert usage["totals"]["input_tokens"] is None and not usage["complete"]["input_tokens"]
        assert usage["complete"]["summed_call_ms"] and usage["totals"]["summed_call_ms"] >= 0
        assert store.get_turn(tid)["cost_usd"] is None
        assert receipt["attempts"][1]["failure_kind"] == "provider_error"
        assert "private failure" not in json.dumps(receipt)


def test_steering_discarded_call_is_not_added_to_ledger_totals_twice(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        provider = SteerOnce(store, sid)
        original = provider.complete

        def metered(messages, *, model):
            return replace(
                original(messages, model=model), input_tokens=3, output_tokens=2, cost_usd=0.01
            )

        provider.complete = metered
        result = loop_with(store, provider).run_turn(sid, "Initial request")
        usage = research_usage.receipt(store, result["turn_id"])
        assert usage["foreground"]["attempt_count"] == 2
        assert store.get_turn(result["turn_id"])["input_tokens"] == 6
        assert store.get_turn(result["turn_id"])["cost_usd"] == pytest.approx(0.02)
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM steering_discarded_answers").fetchone()[0] == 1


class Bounded:
    def __init__(self, name, text="Answer", *, fail=False, model=None, cost=0.01):
        self.name, self.text, self.fail, self.model, self.cost = name, text, fail, model, cost
        self.calls = 0
        self.local_capacity_key = "preserved-local-capability"

    def complete_bounded(self, messages, *, model, timeout):
        self.calls += 1
        if self.fail:
            raise ProviderUnavailable("synthetic provider error")
        return Completion(self.text, self.model or model, self.name, 3, 2, 1, self.cost)


@pytest.mark.parametrize("fallback", [False, True])
def test_routing_and_answer_preserve_each_configured_and_actual_identity(tmp_path, fallback):
    with closing(Store(tmp_path / "pi.db")) as store:
        settings = config()
        settings["providers"].append(dict(id="route-service", name="Router", enabled=True))
        settings["models"].append(
            dict(
                id="route-choice",
                providerId="route-service",
                name="Laya",
                route="model-routing",
                enabled=True,
            )
        )
        settings["roleSettings"]["answerMode"] = "router"
        settings["roleSettings"]["roles"]["routing"].update(
            enabled=True, eligibleModelIds=["route-choice"], modelId="route-choice"
        )
        if fallback:
            settings["roleSettings"]["roles"]["answer"].update(
                failure="fallback", fallbackModelId="b"
            )
        model_roles.save(store, model_roles.Update(expected_revision=0, configuration=settings))
        route = Bounded("decisions", '{"modelId":"a"}', model="laya-reported")
        first, second = (
            Bounded("first-adapter", fail=fallback, model="first-reported"),
            Bounded("second-adapter", model="second-reported"),
        )
        loop = Loop(store, Router(providers={"one": first, "two": second, "route-service": route}))
        tid = loop.run_turn(store.create_session(), "Route this request")["turn_id"]
        receipt = research_usage.receipt(store, tid)
        rows = receipt["attempts"]
        assert [row["role"] for row in rows] == (
            ["routing", "answer", "answer"] if fallback else ["routing", "answer"]
        )
        assert rows[0]["provider_id"] == "route-service" and rows[0]["model_id"] == "route-choice"
        assert rows[0]["provider"] == "decisions" and rows[0]["model"] == "model-routing"
        assert rows[0]["actual_model"] == "laya-reported"
        assert rows[-1]["actual_model"] == ("second-reported" if fallback else "first-reported")
        assert rows[-1]["model_id"] == ("b" if fallback else "a")
        assert receipt["foreground"]["known_subtotal"]["input_tokens"] == 6
        assert receipt["foreground"]["totals"]["input_tokens"] == (None if fallback else 6)
        assert first.local_capacity_key == "preserved-local-capability"


def test_thinking_attempts_do_not_change_foreground_totals_and_missing_cost_stays_unknown(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        tid = store.start_turn(sid)
        execution = session_settings.execution(store, sid, tid)
        answer = research_usage.wrap(store, execution, Bounded("answer", cost=None))
        answer.complete_bounded([], model="answer-model", timeout=1)
        before = store.get_turn(tid)
        thinker = research_usage.wrap(store, execution, Bounded("thinker"), "thinking")
        thinker.complete_bounded([], model="think-model", timeout=1)
        receipt = research_usage.receipt(store, tid)
        assert receipt["foreground"]["totals"]["input_tokens"] == 3
        assert receipt["thinking"]["totals"]["input_tokens"] == 3
        assert receipt["foreground"]["totals"]["cost_usd"] is None
        assert receipt["foreground"]["known_subtotal"]["cost_usd"] == 0
        assert receipt["thinking"]["totals"]["cost_usd"] == 0.01
        assert store.get_turn(tid)["input_tokens"] == before["input_tokens"]
        assert answer.local_capacity_key == "preserved-local-capability"


def test_live_background_completion_cannot_overwrite_talking_totals(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, provider = setup_thinking(store)
        original = provider.complete_bounded

        def metered(messages, *, model, timeout):
            return replace(
                original(messages, model=model, timeout=timeout),
                input_tokens=7 if model == "think" else 3,
                output_tokens=2,
                cost_usd=0.01,
            )

        provider.complete_bounded = metered
        sid = store.create_session()
        tid = loop.run_turn(sid, "Talk and analyze", think=True)["turn_id"]
        assert provider.started.wait(2)
        during = research_usage.receipt(store, tid)
        assert during["foreground"]["totals"]["input_tokens"] == 3
        assert during["thinking"]["pending_count"] == 1
        wait_workers(loop, provider)
        after = research_usage.receipt(store, tid)
        assert after["foreground"]["totals"]["input_tokens"] == 3
        assert after["thinking"]["totals"]["input_tokens"] == 7
        assert store.get_turn(tid)["input_tokens"] == 3


def test_pending_interrupted_attempt_remains_unknown_not_zero(tmp_path):
    class Crash(BaseException):
        pass

    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        tid = store.start_turn(sid)
        observe = research_usage.observer(store, session_settings.execution(store, sid, tid))

        def crash():
            raise Crash()

        with pytest.raises(Crash):
            observe(crash, provider="synthetic", model="model", role="answer")
        store.mark_interrupted_turns()
        receipt = research_usage.receipt(store, tid)
        assert receipt["foreground"]["pending_count"] == 1
        assert all(value is None for value in receipt["foreground"]["totals"].values())
        assert receipt["attempts"][0]["elapsed_ms"] is None


@pytest.mark.parametrize("tokens", [2**31, 2**4000])
def test_invalid_or_unrepresentable_provider_counters_remain_unknown(tmp_path, tokens):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        tid = store.start_turn(sid)
        observe = research_usage.observer(store, session_settings.execution(store, sid, tid))
        observe(
            lambda: Completion("Answer", "actual", "provider", tokens, -2, True, float("nan")),
            provider="provider",
            model="requested",
            role="answer",
        )
        receipt = research_usage.receipt(store, tid)
        assert receipt["foreground"]["reported_count"] == 1
        assert all(receipt["foreground"]["totals"][key] is None for key in research_usage.FIELDS)
        assert all(
            receipt["foreground"]["unknown_counts"][key] == 1 for key in research_usage.FIELDS
        )
        json.dumps(receipt, allow_nan=False)


def test_unrepresentable_sum_has_no_fabricated_subtotal_and_does_not_fail_provider(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        tid = store.start_turn(sid)
        observe = research_usage.observer(store, session_settings.execution(store, sid, tid))
        for _ in range(2):
            result = observe(
                lambda: Completion(
                    "Answer", "actual", "provider", 3, 2, 1, float(research_usage.MAX_SAFE_NUMBER)
                ),
                provider="provider",
                model="requested",
                role="answer",
            )
            assert result.text == "Answer"
        with store._connect() as db:
            db.execute(
                "UPDATE research_model_calls SET elapsed_ms=?", (research_usage.MAX_SAFE_NUMBER,)
            )
        receipt = research_usage.receipt(store, tid)
        summary = receipt["foreground"]
        for field in ("cost_usd", "summed_call_ms"):
            assert summary["known_subtotal"][field] is None
            assert summary["totals"][field] is None
            assert not summary["complete"][field]
            assert summary["unknown_counts"][field] == 0
        assert summary["totals"]["input_tokens"] == 6
        assert type(summary["totals"]["input_tokens"]) is int
        assert store.get_turn(tid)["cost_usd"] is None
        assert summary["reported_count"] == 2
        json.dumps(receipt, allow_nan=False)


def test_malformed_legacy_counters_and_route_labels_cannot_break_new_inference(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        tid = store.start_turn(sid)
        with store._connect() as db:
            db.executemany(
                "INSERT INTO research_model_calls"
                "(id,turn_id,provider,model,role,status,started_at,input_tokens,cost_usd) "
                "VALUES(?,?,?,?,'answer','reported',1,?,?)",
                [(f"old_{i}", tid, "p" * 101, "m" * 301, 2**63 - 1, 1e308) for i in range(2)],
            )
        observe = research_usage.observer(store, session_settings.execution(store, sid, tid))
        observe(
            lambda: Completion("Answer", "a" * 301, "p" * 101, 3, 2),
            provider="p" * 101,
            model="m" * 301,
            role="answer",
        )
        receipt = research_usage.receipt(store, tid)
        summary = receipt["foreground"]
        assert summary["known_subtotal"]["input_tokens"] == 3
        assert summary["unknown_counts"]["input_tokens"] == 2
        assert summary["totals"]["input_tokens"] is None
        assert all(
            row["provider"] == "unknown" and row["model"] == "unknown"
            for row in receipt["attempts"]
        )
        assert receipt["attempts"][-1]["actual_model"] is None
        assert receipt["attempts"][0]["input_tokens"] is None
        assert receipt["attempts"][0]["cost_usd"] is None
        json.dumps(receipt, allow_nan=False)


def test_configured_memory_ranker_records_a_distinct_foreground_role(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        settings = config()
        settings["roleSettings"]["roles"]["memory-ranking"].update(
            enabled=True, eligibleModelIds=["b"], modelId="b"
        )
        model_roles.save(store, model_roles.Update(expected_revision=0, configuration=settings))
        sid = store.create_session()
        tid = store.start_turn(sid)
        provider = Bounded("ranker", '{"order":["m1","m0"]}', model="ranker-reported")
        ranker = ConfiguredMemoryRanker(store, {"two": provider})
        package = {
            "memories": [{"id": "a", "text": "first"}, {"id": "b", "text": "second"}],
            "retrieval": {},
        }
        result = ranker.rank_for_turn("query", package, session_settings.execution(store, sid, tid))
        assert result["memories"] == list(reversed(package["memories"]))
        row = research_usage.receipt(store, tid)["attempts"][0]
        assert (row["role"], row["lane"], row["model_id"]) == ("memory-ranking", "foreground", "b")


def test_legacy_migration_retains_old_rows_and_marks_only_new_turns_covered(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        old = store.start_turn(sid)
        with store._connect() as db:
            db.execute("UPDATE turns SET status='failed',input_tokens=17 WHERE id=?", (old,))
            db.execute(
                "INSERT INTO research_model_calls(id,turn_id,provider,model,role,status,started_at,input_tokens) VALUES('legacy',?,'adapter','requested','answer','reported',1,17)",
                (old,),
            )
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TRIGGER new_turn_usage_version")
        db.execute("ALTER TABLE turns DROP COLUMN usage_version")
        for column in research_usage.EXTRA_COLUMNS:
            db.execute(f"ALTER TABLE research_model_calls DROP COLUMN {column}")
        db.commit()
    with closing(Store(path)) as store:
        legacy = research_usage.receipt(store, old)
        assert legacy["coverage"] == "legacy_partial"
        assert legacy["foreground"]["known_subtotal"]["input_tokens"] == 17
        assert legacy["foreground"]["totals"]["input_tokens"] is None
        assert legacy["attempts"][0]["actual_model"] is None
        assert legacy["attempts"][0]["model"] == "requested"
        assert store.get_turn(old)["input_tokens"] == 17
        research_usage.wrap(
            store, session_settings.execution(store, sid, old), Bounded("resumed")
        ).complete_bounded([], model="new-model", timeout=1)
        resumed = research_usage.receipt(store, old)
        assert resumed["coverage"] == "legacy_partial"
        assert resumed["foreground"]["known_subtotal"]["input_tokens"] == 20
        assert resumed["foreground"]["totals"]["input_tokens"] is None
        assert store.get_turn(old)["input_tokens"] is None
        new = store.start_turn(sid)
        empty = research_usage.receipt(store, new)
        assert (
            empty["coverage"] == "complete" and empty["foreground"]["totals"]["input_tokens"] == 0
        )
        assert empty["foreground"]["complete"]["cost_usd"]


def test_usage_read_is_bounded_authorized_and_forgotten_data_is_unavailable(tmp_path, monkeypatch):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        tid = store.start_turn(sid)
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "INSERT INTO research_model_calls(id,turn_id,provider,model,role,status,started_at,input_tokens) VALUES(?,?,'adapter','model','answer','reported',?,1)",
                [(f"{number:032x}", tid, number) for number in range(105)],
            )
            db.commit()
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "synthetic-recovery", raising=False)
        monkeypatch.setattr(
            api.app.state,
            "gateway_key_hash",
            hashlib.sha256(b"synthetic-runtime").hexdigest(),
            raising=False,
        )
        client = TestClient(api.app)
        url, headers = f"/turns/{tid}/usage", {"X-Pi-Gateway-Key": "synthetic-runtime"}
        assert client.get(url).status_code == 401
        assert client.get(url + "?limit=100000", headers=headers).status_code == 422
        response = client.get(url, headers=headers)
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["attempts_truncated"] and len(receipt["attempts"]) == 100
        assert receipt["foreground"]["attempt_count"] == 105
        assert receipt["foreground"]["totals"]["input_tokens"] == 105
        assert [row["started_at"] for row in receipt["attempts"]] == list(range(5, 105))
    plan = forgetting.preview(path, sid)
    forgetting.forget(path, sid, plan["confirmation"])
    with closing(Store(path)) as store:
        monkeypatch.setattr(api.app.state, "store", store)
        assert client.get(url, headers=headers).status_code == 404
