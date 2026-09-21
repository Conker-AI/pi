"""Real Loop context selection uses one frozen, validated helper result."""

import json
import sqlite3
from contextlib import closing

import pytest

from pi import attachments, forgetting, model_roles, session_settings, submissions
from pi import context_controls as c
from pi import context_retrieval as r
from pi.loop import Loop
from pi.providers import Completion
from pi.routing import Router
from pi.store import Store


def config():
    disabled = dict(
        enabled=False,
        eligibleModelIds=[],
        modelId=None,
        timeoutMs=1000,
        failure="stop",
        fallbackModelId=None,
    )

    def assignment(identity):
        return {
            **disabled,
            "enabled": True,
            "eligibleModelIds": [identity],
            "modelId": identity,
        }

    return model_roles.Configuration.model_validate(
        {
            "providers": [{"id": "test", "name": "Test", "enabled": True}],
            "models": [
                {"id": i, "providerId": "test", "name": i, "route": i, "enabled": True}
                for i in ("selector", "answer")
            ],
            "defaultModelId": "answer",
            "roleSettings": {
                "answerMode": "manual",
                "roles": {
                    **{role: dict(disabled) for role in model_roles.ROLES},
                    "answer": assignment("answer"),
                    "context-selection": assignment("selector"),
                },
            },
        }
    )


class Provider:
    name = "test"

    def __init__(self, selected=None, output=None, callback=None):
        self.selected, self.output, self.callback, self.calls = selected or [], output, callback, []

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append((model, messages))
        if model == "selector" and self.callback:
            self.callback()
        text = (
            (self.output if self.output is not None else json.dumps({"messageIds": self.selected}))
            if model == "selector"
            else "Answer"
        )
        return Completion(text=text, model=model, provider=self.name)


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "test.db")) as value:
        model_roles.save(value, model_roles.Update(expected_revision=0, configuration=config()))
        yield value


def policy(store, sid, modes, window=10000):
    value = c.Policy(
        sessionInstructions="Owner instruction",
        messagePolicies=modes,
        budget=c.Budget(contextWindowTokens=window, outputReserveTokens=10, otherInputTokens=0),
    )
    c.save(store, sid, c.Update(expected_revision=c.load(store, sid)["revision"], policy=value))


def loop(store, provider):
    return Loop(store, Router(local_provider=provider, local_model="unused"))


def test_selection_query_pins_exclusions_frozen_policy_and_repeated_history(store):
    sid = store.create_session()
    rows = [
        store.append_message(sid, "user", text)
        for text in ("Exact pin", "Relevant candidate", "Other candidate", "Excluded secret")
    ]
    pin, selected, other, excluded = [row["id"] for row in rows]
    policy(
        store,
        sid,
        {pin: "keep-exact", selected: "retrieve", other: "retrieve", excluded: "exclude"},
    )

    def change_policy():
        policy(store, sid, {pin: "exclude", selected: "exclude", excluded: "keep-exact"})
        newer = config()
        newer.models[1].route = "later-answer"
        model_roles.save(store, model_roles.Update(expected_revision=1, configuration=newer))

    provider = Provider([selected], callback=change_policy)
    runtime = loop(store, provider)
    result = runtime.run_turn(
        sid, "Which candidate is relevant?", request_id="retrieval_request_001"
    )
    assert [call[0] for call in provider.calls] == ["selector", "answer"]
    helper_text = provider.calls[0][1][-1].content
    assert "Which candidate is relevant?" in helper_text
    assert "Exact pin" not in helper_text and "Excluded secret" not in helper_text
    answer = [message.content for message in provider.calls[1][1]]
    assert "Exact pin" in answer and "Relevant candidate" in answer
    assert "Other candidate" not in answer and "Excluded secret" not in answer
    for _ in range(3):
        runtime._history(sid, turn_id=result["turn_id"])
    assert runtime.run_turn(
        sid, "Which candidate is relevant?", request_id="retrieval_request_001"
    )["replayed"]
    assert len(provider.calls) == 2
    assert c.load(store, sid, result["turn_id"])["policy"]["messagePolicies"][pin] == "keep-exact"
    receipt = r.read(store, sid, turn_id=result["turn_id"])
    assert (
        receipt["selectedIds"] == [selected] and receipt["evidence"]["configurationRevision"] == 1
    )


@pytest.mark.parametrize(
    "output",
    [
        '{"messageIds":["foreign"]}',
        '{"messageIds":[],"extra":true}',
        '{"messageIds":[],"messageIds":[]}',
        "not JSON",
    ],
)
def test_invalid_selection_fails_without_answer_or_automatic_retry(store, output):
    sid = store.create_session()
    candidate = store.append_message(sid, "user", "Candidate")
    policy(store, sid, {candidate["id"]: "retrieve"})
    provider = Provider(output=output)
    runtime = loop(store, provider)
    with pytest.raises(c.ContextError):
        runtime.run_turn(sid, "Query", request_id="invalid_selection_001")
    assert [call[0] for call in provider.calls] == ["selector"]
    assert runtime.run_turn(sid, "Query", request_id="invalid_selection_001")["replayed"]
    assert (
        len(provider.calls) == 1
        and r.read(store, sid, request_id="invalid_selection_001")["state"] == "failed"
    )


def test_empty_valid_selection_keeps_exact_pin(store):
    sid = store.create_session()
    pin = store.append_message(sid, "user", "Pinned")
    candidate = store.append_message(sid, "assistant", "Optional")
    policy(store, sid, {pin["id"]: "keep-exact", candidate["id"]: "retrieve"})
    provider = Provider([])
    loop(store, provider).run_turn(sid, "Query")
    answer = [message.content for message in provider.calls[1][1]]
    assert "Pinned" in answer and "Optional" not in answer


@pytest.mark.parametrize("historical", [False, True])
def test_harness_privacy_blocks_current_or_historical_candidate(store, historical):
    sid = store.create_session()

    def privacy(disabled):
        session_settings.save(
            store,
            sid,
            session_settings.Update(
                expected_revision=session_settings.load(store, sid)["revision"],
                settings=session_settings.Settings(
                    agentId="companion",
                    privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=disabled),
                ),
            ),
        )

    privacy(True)
    candidate = store.append_message(sid, "user", "Harness excluded source")
    if historical:
        privacy(False)
    policy(store, sid, {candidate["id"]: "retrieve"})
    provider = Provider([candidate["id"]])
    with pytest.raises(c.ContextError):
        loop(store, provider).run_turn(sid, "Query")
    assert not provider.calls


def test_fixed_pin_budget_failure_does_not_call_helper(store):
    sid = store.create_session()
    pin = store.append_message(sid, "user", "x" * 500)
    candidate = store.append_message(sid, "assistant", "Optional")
    policy(store, sid, {pin["id"]: "keep-exact", candidate["id"]: "retrieve"}, window=50)
    provider = Provider([candidate["id"]])
    with pytest.raises(c.ContextError, match="not dropped"):
        loop(store, provider).run_turn(sid, "Query")
    assert not provider.calls


def test_interrupted_helper_restart_never_reexecutes(tmp_path):
    class Crash(BaseException):
        pass

    path = tmp_path / "test.db"
    with closing(Store(path)) as store:
        model_roles.save(store, model_roles.Update(expected_revision=0, configuration=config()))
        sid = store.create_session()
        candidate = store.append_message(sid, "user", "Candidate")
        policy(store, sid, {candidate["id"]: "retrieve"})
        provider = Provider(callback=lambda: (_ for _ in ()).throw(Crash()))
        with pytest.raises(Crash):
            loop(store, provider).run_turn(sid, "Query", request_id="interrupted_helper_001")
    with closing(Store(path)) as store:
        store.mark_interrupted_turns()
        assert r.recover_interrupted(store) == 1
        provider = Provider()
        assert loop(store, provider).run_turn(sid, "Query", request_id="interrupted_helper_001")[
            "replayed"
        ]
        assert not provider.calls
        assert r.read(store, sid, request_id="interrupted_helper_001")["state"] == "interrupted"


def test_failed_preparation_and_attachment_release_are_atomic(store):
    sid = store.create_session()
    attachment = attachments.upload(
        store,
        sid,
        attachments.Metadata(name="note.txt", type="text/plain"),
        b"note",
        resolve=session_settings.source_privacy,
    )["id"]
    request = "atomic_preparation_001"
    submissions.reserve(store, request, sid, "Query", {}, attachment_ids=[attachment])
    with store._connect() as db:
        db.execute(
            "CREATE TRIGGER fail_attachment_release BEFORE DELETE ON "
            "attachment_reservations BEGIN SELECT RAISE(ABORT,'simulated "
            "cleanup failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="cleanup"):
        submissions.fail_preparation(store, request)
    assert submissions.get(store, request)["state"] == "preparing"
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM attachment_reservations").fetchone()[0] == 1
        db.execute("DROP TRIGGER fail_attachment_release")
    with store._connect() as db:
        # Old autocommit crash boundary: terminal receipt with a stale reservation.
        db.execute(
            "UPDATE turn_submissions SET state='preparation_failed' WHERE request_id=?", (request,)
        )
        submissions.recover_preparations(db)
        assert db.execute("SELECT COUNT(*) FROM attachment_reservations").fetchone()[0] == 0


def test_selected_history_budget_failure_does_not_dispatch_answer(store):
    sid = store.create_session()
    session_settings.save(
        store,
        sid,
        session_settings.Update(
            expected_revision=0,
            settings=session_settings.Settings(
                agentId="companion",
                privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=False),
            ),
        ),
    )
    candidate = store.append_message(sid, "user", "Relevant " * 50)
    policy(store, sid, {candidate["id"]: "retrieve"}, window=50)
    provider = Provider([candidate["id"]])
    with pytest.raises(c.ContextError, match="not dropped"):
        loop(store, provider).run_turn(sid, "Query", request_id="retrieval_budget_001")
    # No-memory and no-harness are independent: helper ran, but answer budget failed.
    assert [call[0] for call in provider.calls] == ["selector"]
    assert store.get_message(candidate["id"])["content"] == "Relevant " * 50
    with store._connect() as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            "UPDATE submission_context SET selected='[]' WHERE request_id='retrieval_budget_001'"
        )


def test_offline_forgetting_purges_frozen_context_instructions(tmp_path):
    path = tmp_path / "test.db"
    secret = "unique-frozen-context-secret-761299"
    with closing(Store(path)) as store:
        model_roles.save(store, model_roles.Update(expected_revision=0, configuration=config()))
        sid = store.create_session()
        candidate = store.append_message(sid, "user", "Candidate")
        value = c.Policy(
            sessionInstructions=secret,
            messagePolicies={candidate["id"]: "retrieve"},
            budget=c.Budget(contextWindowTokens=1000, outputReserveTokens=10, otherInputTokens=0),
        )
        c.save(store, sid, c.Update(expected_revision=0, policy=value))
        loop(store, Provider([candidate["id"]])).run_turn(
            sid, "Query", request_id="forget_context_helper_001"
        )
    forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])
    assert secret.encode() not in path.read_bytes()
    with closing(Store(path)) as store, store._connect() as db:
        row = db.execute(
            "SELECT policy,state FROM submission_context WHERE "
            "request_id='forget_context_helper_001'"
        ).fetchone()
        assert row[0] is None and row[1] == "forgotten"


def test_owner_stop_discards_late_selection_and_never_dispatches_answer(store):
    from pi import turn_control
    sid = store.create_session()
    candidate = store.append_message(sid, "user", "Candidate text")
    policy(store, sid, {candidate["id"]: "retrieve"})
    provider = Provider([candidate["id"]], callback=lambda: turn_control.cancel_submission(
        store, "selection_cancel_001"))
    result = loop(store, provider).run_turn(sid, "Choose context", request_id="selection_cancel_001")
    assert result["status"] == "cancelled" and result["turn_id"] is None
    assert [call[0] for call in provider.calls] == ["selector"]
    receipt = r.read(store, sid, request_id="selection_cancel_001")
    assert receipt["state"] == "interrupted" and not receipt["selectedIds"]
    assert len(store.messages(sid)) == 1


def test_reply_to_unselected_retrieval_candidate_requires_context_review(store):
    sid = store.create_session()
    target = store.append_message(sid, "assistant", "Candidate answer")
    policy(store, sid, {target["id"]: "retrieve"})
    provider = Provider([])
    with pytest.raises(c.ContextError, match="reply target"):
        loop(store, provider).run_turn(sid, "Explain this", request_id="reply_retrieval_001",
                                      reply_to=target["id"])
    assert [call[0] for call in provider.calls] == ["selector"]
    assert len(store.messages(sid)) == 1
