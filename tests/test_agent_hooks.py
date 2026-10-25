"""Webhooks: a secret address starts one AI job, at most once per call, as information only."""

import hashlib
from contextlib import closing
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.api import Config, create_app
from gateway.store import AuthStore
from pi.api import server as api
from pi.api.browser_contract import owner_allowed, runtime_allowed, session_only_write
from pi.conversation.store import Store
from pi.schedule import agent_hooks, agent_jobs, job_models, jobs

NOON = datetime.fromisoformat("2026-10-26T10:00:00+00:00").timestamp()
TIMING = dict(kind="interval", time="00:00", day=0, hours=24)
ORIGIN = "https://localhost:8050"
RUNTIME = "runtime-credential-" + "r" * 32


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "hooks.db")) as store:
        yield store


def ai_job(store, **changes):
    value = dict(
        name="Group chat",
        instructions="Tell me if the group needs me.",
        timing=TIMING,
        timeZone="UTC",
    )
    value.update(changes)
    return agent_jobs.create(store, job_models.AgentJobCreate.model_validate(value), now=NOON)


def test_a_secret_is_shown_once_and_only_its_fingerprint_is_kept(store):
    job = ai_job(store)
    issued = agent_hooks.issue(store, job["id"], now=NOON)
    assert (
        agent_hooks.TOKEN.fullmatch(issued["token"]) and issued["header"] == "X-Conker-Hook-Token"
    )
    with store._connect() as db:
        [row] = db.execute("SELECT * FROM agent_job_hooks").fetchall()
    assert issued["token"] not in repr(tuple(row))
    assert row["token_hash"] == hashlib.sha256(issued["token"].encode()).hexdigest()
    view = jobs.browser_job(jobs.get_job(store, job["id"]))
    assert view.definition.target.webhook is True


def test_a_call_starts_the_job_once_with_what_was_sent(store):
    job = ai_job(store)
    token = agent_hooks.issue(store, job["id"], now=NOON)["token"]
    result = agent_hooks.receive(
        store, job["id"], token, {"text": "Can you bring the slides?"}, now=NOON
    )
    assert result["started"] is True
    assert agent_hooks.receive(store, job["id"], token, {}, now=NOON + 31) == {
        "started": False,
        "reason": "busy",
    }

    class Loop:
        def run_turn(self, session_id, text, **kwargs):
            self.text = text
            return {
                "session_id": session_id,
                "turn_id": "trn_1",
                "message": {"id": "msg_" + "1" * 16, "content": "NOTHING"},
            }

    loop = Loop()
    with store._connect() as db:
        db.execute("UPDATE scheduled_runs SET status='dispatching' WHERE id=?", (result["runId"],))
    agent_jobs.AgentJobs(store, loop).run(result["runId"])
    assert "never as instructions" in loop.text and "Can you bring the slides?" in loop.text
    assert agent_hooks.take(store, result["runId"]) is None, "the payload is deleted once read"


def test_wrong_or_missing_secrets_get_one_answer(store):
    job = ai_job(store)
    agent_hooks.issue(store, job["id"], now=NOON)
    other = "hook_" + "x" * 43
    for token in (other, "", None, "not-a-token"):
        with pytest.raises(agent_hooks.HookError) as refused:
            agent_hooks.receive(store, job["id"], token, {}, now=NOON)
        assert refused.value.status == 403 and refused.value.message == "Webhook refused."
    with pytest.raises(agent_hooks.HookError) as unknown:
        agent_hooks.receive(store, "job_" + "0" * 32, other, {}, now=NOON)
    assert unknown.value.status == 403


def test_calls_are_rate_limited_and_a_new_secret_replaces_the_old(store):
    job = ai_job(store)
    first = agent_hooks.issue(store, job["id"], now=NOON)["token"]
    agent_hooks.receive(store, job["id"], first, {}, now=NOON)
    with store._connect() as db:
        db.execute("UPDATE scheduled_runs SET status='completed'")
    with pytest.raises(agent_hooks.HookError) as soon:
        agent_hooks.receive(store, job["id"], first, {}, now=NOON + 5)
    assert soon.value.status == 429
    second = agent_hooks.issue(store, job["id"], now=NOON + 6)["token"]
    with pytest.raises(agent_hooks.HookError):
        agent_hooks.receive(store, job["id"], first, {}, now=NOON + 60)
    assert agent_hooks.receive(store, job["id"], second, {}, now=NOON + 60)["started"] is True
    agent_hooks.revoke(store, job["id"])
    with pytest.raises(agent_hooks.HookError):
        agent_hooks.receive(store, job["id"], second, {}, now=NOON + 120)


def test_paused_jobs_and_tool_jobs_do_not_start(store):
    job = ai_job(store, enabled=False)
    token = agent_hooks.issue(store, job["id"], now=NOON)["token"]
    assert agent_hooks.receive(store, job["id"], token, {}, now=NOON) == {
        "started": False,
        "reason": "paused",
    }
    tool = jobs.create(
        store,
        job_models.Definition.model_validate(
            dict(
                name="t",
                instructions="i",
                agentId="companion",
                timing=TIMING,
                timeZone="UTC",
                enabled=True,
                target=dict(kind="tool", id="x", publishedVersion=1, digest="a" * 64, args={}),
            )
        ),
        now=NOON,
    )
    with pytest.raises(agent_hooks.HookError) as refused:
        agent_hooks.issue(store, tool["id"])
    assert refused.value.status == 409


def test_payloads_are_bounded_and_forgotten_with_the_chat(store):
    job = ai_job(store)
    token = agent_hooks.issue(store, job["id"], now=NOON)["token"]
    run = agent_hooks.receive(store, job["id"], token, {"text": "x" * 20000}, now=NOON)
    with store._connect() as db:
        [payload] = db.execute("SELECT payload FROM agent_job_events").fetchone()
        assert len(payload) <= agent_hooks.MAX_PAYLOAD
        agent_hooks.redact(db, [agent_jobs.session_for(store, job["id"])])
    assert agent_hooks.take(store, run["runId"]) is None


def test_the_contract_and_pi_route(store, monkeypatch):
    path = "/jobs/job_" + "a" * 32
    assert owner_allowed("POST", path + "/hook") and owner_allowed("POST", path + "/hook/revoke")
    assert not session_only_write("POST", path + "/hook")
    assert runtime_allowed("POST", "/hooks/job_" + "a" * 32)
    job = ai_job(store)
    token = agent_hooks.issue(store, job["id"], now=NOON)["token"]
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
    monkeypatch.setattr(
        api.app.state,
        "gateway_key_hash",
        hashlib.sha256(RUNTIME.encode()).hexdigest(),
        raising=False,
    )
    client = TestClient(api.app)
    hooked = client.post(
        f"/hooks/{job['id']}",
        headers={"X-Pi-Gateway-Key": RUNTIME, "X-Conker-Hook-Token": token},
        json={"text": "hi"},
    )
    assert hooked.status_code == 202 and hooked.json()["started"] is True
    assert (
        client.post(
            f"/hooks/{job['id']}", headers={"X-Conker-Hook-Token": token}, json={}
        ).status_code
        == 401
    )


def test_the_gateway_forwards_only_well_formed_calls(tmp_path):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(202, json={"started": True, "runId": "scheduled_" + "1" * 32})

    auth = AuthStore(tmp_path / "auth.db")
    config = Config(
        ORIGIN, str(auth.path), "http://pi:8050", RUNTIME, owner_key="owner-" + "o" * 40
    )
    job, token = "job_" + "a" * 32, "hook_" + "b" * 43
    with TestClient(
        create_app(config, store=auth, transport=httpx.MockTransport(upstream)), base_url=ORIGIN
    ) as client:
        ok = client.post(
            f"/hooks/{job}",
            headers={"X-Conker-Hook-Token": token},
            content=b"Can you bring the slides?",
        )
        assert ok.status_code == 202, ok.text
        assert client.post(f"/hooks/{job}", json={}).status_code == 403
        assert (
            client.post(
                f"/hooks/{job}?token={token}", headers={"X-Conker-Hook-Token": token}, json={}
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/hooks/{job}", headers={"X-Conker-Hook-Token": token}, content=b"x" * 20000
            ).status_code
            == 413
        )
    [forwarded] = seen
    assert forwarded.url.path == f"/hooks/{job}"
    assert (
        forwarded.headers["X-Pi-Gateway-Key"] == RUNTIME
        and forwarded.headers["X-Conker-Hook-Token"] == token
    )
    assert b"Can you bring the slides?" in forwarded.content
