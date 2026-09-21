"""Offline recovery drill against copies of temporary Pi databases only."""

import sqlite3
from contextlib import closing

import pytest
from test_jobs import definition
from test_loop import Recorder, loop_with

from pi import attachments, context_retrieval, drafts, jobs, session_settings, submissions
from pi.job_worker import JobWorker
from pi.store import Store


def snapshot(source, destination):
    # SQLite's backup API includes committed WAL pages; copying just pi.db does not.
    with (
        closing(sqlite3.connect(source)) as original,
        closing(sqlite3.connect(destination)) as restored,
    ):
        original.backup(restored)
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("PRAGMA foreign_key_check").fetchall() == []


def test_backup_recovers_transcript_draft_attachment_and_request_identity(tmp_path):
    source, copy = tmp_path / "source.db", tmp_path / "restored.db"
    request = "restored_completed_request"
    with closing(Store(source)) as store:
        sid = store.create_session()
        provider = Recorder("Saved answer")
        result = loop_with(store, provider).run_turn(sid, "Hello", request_id=request)
        drafts.save(store, sid, drafts.Save(expected_revision=0, text="Unsent next question"))
        upload = attachments.upload(
            store,
            sid,
            attachments.Metadata(name="notes.txt", type="text/plain"),
            b"Original notes",
            resolve=session_settings.source_privacy,
        )
        before = store.messages(sid)
        snapshot(source, copy)

    with closing(Store(copy)) as restored:
        assert restored.mark_interrupted_turns() == 0
        context_retrieval.recover_interrupted(restored)
        assert restored.messages(sid) == before
        assert drafts.load(restored, sid)["text"] == "Unsent next question"
        with restored._connect() as db:
            row = db.execute(
                "SELECT content,sha256 FROM attachments WHERE id=?", (upload["id"],)
            ).fetchone()
            assert row["content"] == b"Original notes"
            assert row["sha256"] == upload["sha256"]
        offline_provider = Recorder(fail=True)
        replay = loop_with(restored, offline_provider).run_turn(sid, "Hello", request_id=request)
        assert replay["replayed"] and replay["turn_id"] == result["turn_id"]
        assert offline_provider.calls == []


def test_restore_preserves_uncertainty_and_does_not_restart_effects(tmp_path):
    source, copy = tmp_path / "source.db", tmp_path / "restored.db"
    with closing(Store(source)) as store:
        sid = store.create_session()
        pending = "restored_preparing_request"
        submissions.reserve(store, pending, sid, "Not dispatched", {})
        job = jobs.create(store, definition(), now=0)
        run = jobs.run_now(store, job["id"], "restored_uncertain_job", now=1)
        # Persisted pre-network claim is deliberately ambiguous after a crash.
        with store._connect() as db:
            db.execute("UPDATE scheduled_runs SET status='dispatching' WHERE id=?", (run["id"],))
        snapshot(source, copy)

    with closing(Store(copy)) as restored:
        restored.mark_interrupted_turns()
        context_retrieval.recover_interrupted(restored)
        offline_provider = Recorder(fail=True)
        replay = loop_with(restored, offline_provider).run_turn(
            sid, "Not dispatched", request_id=pending
        )
        assert replay["replayed"] and offline_provider.calls == []
        assert replay["submission"]["state"] == "preparation_interrupted"
        effects = []
        worker = JobWorker(restored, lambda *args, **kwargs: effects.append(kwargs))
        worker.tick(now=3600)
        assert effects == []
        assert jobs.runs(restored, job["id"])[0]["status"] == "dispatching"


def test_restored_auth_requires_revocation_before_serving(tmp_path):
    from gateway.store import AuthError, AuthStore

    password = "synthetic recovery drill passphrase"
    source = AuthStore(tmp_path / "auth-source.db")
    source.set_password(password, initial=True)
    anonymous = source.anonymous()
    session = source.login(anonymous["token"], password, "local")
    proof = source.verify(session["token"], password, "local", "restore-drill")
    target = tmp_path / "auth-restored.db"
    snapshot(source.path, target)

    restored = AuthStore(target)
    # A database copy retains valid authority: startup alone is not a restore gate.
    assert restored.session(session["token"])["authenticated"]
    restored.revoke()  # Same primitive as the host's documented revoke-all command.
    restarted = AuthStore(target)
    with pytest.raises(AuthError):
        restarted.session(session["token"])
    with pytest.raises(AuthError):
        restarted.consume(session["token"], proof["verification_token"], "restore-drill")
    with restarted.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM verification_proofs").fetchone()[0] == 0
    fresh = restarted.anonymous()
    assert restarted.login(fresh["token"], password, "local")["authenticated"]
