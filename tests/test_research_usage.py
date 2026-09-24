from contextlib import closing
from dataclasses import replace

import pytest
from test_deep_research import FOLLOWUP, PLAN
from test_tool_turns import build
from test_web_research import SEARCH, SearchGate

from pi import research, research_usage, session_settings
from pi.loop import ActedWithoutReply
from pi.providers import ProviderUnavailable
from pi.store import Store


def priced(provider, unknown=False):
    complete = provider.complete

    def call(messages, *, model):
        result = complete(messages, model=model)
        return replace(
            result,
            input_tokens=10,
            output_tokens=5,
            cached_tokens=2,
            cost_usd=None if unknown else 0.01,
        )

    provider.complete = call


def test_deep_usage_sums_plan_query_followup_and_synthesis_after_reopen(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        loop, provider = build(store, [PLAN, SEARCH, FOLLOWUP, "Answer"], SearchGate())
        priced(provider)
        tid = loop.run_turn(store.create_session(), "Investigate", research_mode="deep")["turn_id"]
    with closing(Store(path)) as store:
        turn = store.get_turn(tid)
        assert turn["input_tokens"] == 40 and turn["output_tokens"] == 20
        assert turn["cached_tokens"] == 8 and turn["cost_usd"] == pytest.approx(0.04)
        usage = research.receipt(store, tid)["usage"]
        assert len(usage["attempts"]) == 4 and usage["cost_usd"] == pytest.approx(0.04)
        assert all("content" not in attempt for attempt in usage["attempts"])


def test_failed_call_remains_unknown_after_successful_recovery(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, provider = build(
            store, [SEARCH, ProviderUnavailable("offline"), "Answer"], SearchGate()
        )
        priced(provider)
        with pytest.raises(ActedWithoutReply) as failed:
            loop.run_turn(store.create_session(), "Search", research_mode="web")
        loop.resume_turn(failed.value.turn_id)
        usage = research.receipt(store, failed.value.turn_id)["usage"]
        assert [row["status"] for row in usage["attempts"]] == ["reported", "failed", "reported"]
        assert usage["cost_usd"] is None
        assert store.get_turn(failed.value.turn_id)["cost_usd"] is None


def test_missing_cost_does_not_erase_known_token_totals(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, provider = build(store, [SEARCH, "Answer"], SearchGate())
        priced(provider, unknown=True)
        tid = loop.run_turn(store.create_session(), "Search", research_mode="web")["turn_id"]
        assert store.get_turn(tid)["input_tokens"] == 20
        assert store.get_turn(tid)["cost_usd"] is None


def test_approval_pause_and_resume_total_without_overwrite(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, provider = build(store, [SEARCH, "Answer"], SearchGate(needs_approval=True))
        priced(provider)
        tid = loop.run_turn(store.create_session(), "Search", research_mode="web")["turn_id"]
        assert store.get_turn(tid)["cost_usd"] == pytest.approx(0.01)
        loop.resume_turn(tid)
        assert store.get_turn(tid)["cost_usd"] == pytest.approx(0.02)


def test_process_interruption_leaves_pending_attempt_not_zero_cost(tmp_path):
    class Crash(BaseException):
        pass

    class CrashingProvider:
        name = "crashing"

        def complete(self, messages, *, model):
            raise Crash()

    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        tid = store.start_turn(sid)
        execution = session_settings.execution(store, sid, turn_id=tid)
        execution["researchMode"] = "web"
        with pytest.raises(Crash):
            research_usage.wrap(store, execution, CrashingProvider()).complete([], model="test")
        with store._connect() as db:
            assert db.execute("SELECT status FROM research_model_calls").fetchone()[0] == "pending"
            assert research_usage.totals(db, tid)["cost_usd"] is None


def test_configured_model_dispatch_preserves_timeout_and_usage(tmp_path):
    from test_model_roles import config

    from pi import model_roles
    from pi.providers import Completion, Message

    class Bounded:
        name = "one"
        calls = []

        def complete_bounded(self, messages, *, model, timeout):
            self.calls.append((model, timeout))
            return Completion(
                text="Answer",
                provider=self.name,
                model=model,
                input_tokens=11,
                output_tokens=4,
                cost_usd=0.02,
            )

    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        tid = store.start_turn(sid)
        execution = session_settings.execution(store, sid, turn_id=tid)
        execution["researchMode"] = "web"
        provider = Bounded()
        wrapped = research_usage.wrap(store, execution, provider)
        result = model_roles.dispatch(
            config(), "answer", [Message("user", "Research")], {"one": wrapped}
        )
        assert result["completion"].text == "Answer"
        assert provider.calls == [("actual-a", 1.0)]
        with store._connect() as db:
            totals = research_usage.totals(db, tid)
            assert totals["input_tokens"] == 11 and totals["cost_usd"] == 0.02


def test_configured_timeout_cannot_fall_back_to_unbounded_call(tmp_path):
    class Unbounded:
        name = "one"

        def complete(self, *args, **kwargs):
            pytest.fail("Must not bypass configured deadline")

    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        tid = store.start_turn(sid)
        execution = session_settings.execution(store, sid, turn_id=tid)
        execution["researchMode"] = "web"
        with pytest.raises(ProviderUnavailable, match="configured timeout"):
            research_usage.wrap(store, execution, Unbounded()).complete_bounded(
                [], model="test", timeout=1
            )
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM research_model_calls").fetchone()[0] == 0
