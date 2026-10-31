"""Pi calls ToolGate as the running agent, so ToolGate enforces each agent's toolkit."""

import httpx
import pytest

from pi.actions import toolgate as gate_module
from pi.actions.toolgate import ToolGateClient, ToolGateUnavailable
from pi.turns.model_calls import identity_scopes


class Recorder:
    def __init__(self, put_status=200):
        self.calls = []
        self.put_status = put_status

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs["headers"], kwargs.get("json")))
        return httpx.Response(self.put_status, json={}, request=httpx.Request("PUT", url))

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs["headers"], None))
        body = []
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))


@pytest.fixture
def recorder(monkeypatch):
    value = Recorder()
    monkeypatch.setattr(gate_module.httpx, "put", value.put)
    monkeypatch.setattr(gate_module.httpx, "get", value.get)
    return value


def test_an_agent_toolkit_is_defined_once_then_every_call_names_the_agent(recorder):
    client = ToolGateClient("http://toolgate.test", "tgx_" + "k" * 30)
    agent = client.acting_as("agent_" + "a" * 32, ["notes.read", "notes.read", "mail.send"])
    assert recorder.calls[0][:2] == (
        "PUT",
        "http://toolgate.test/v2/agent/identities/agent_" + "a" * 32,
    )
    assert recorder.calls[0][3] == {"scopes": ["mail.send", "notes.read"]}
    agent.tools()
    assert recorder.calls[-1][2]["X-ToolGate-Agent"] == "agent_" + "a" * 32
    # The same toolkit is not sent again; the key itself still calls without an agent.
    client.acting_as("agent_" + "a" * 32, ["mail.send", "notes.read"])
    assert [call[0] for call in recorder.calls].count("PUT") == 1
    client.tools()
    assert "X-ToolGate-Agent" not in recorder.calls[-1][2]


def test_an_older_toolgate_keeps_the_key_and_pi_keeps_filtering(monkeypatch):
    old = Recorder(put_status=404)
    monkeypatch.setattr(gate_module.httpx, "put", old.put)
    client = ToolGateClient("http://toolgate.test", "tgx_" + "k" * 30)
    assert client.acting_as("companion", ["*"]) is client


def test_toolgate_refusing_the_toolkit_is_unavailable_not_silent(monkeypatch):
    refused = Recorder(put_status=403)
    monkeypatch.setattr(gate_module.httpx, "put", refused.put)
    with pytest.raises(ToolGateUnavailable):
        ToolGateClient("http://toolgate.test", "tgx_" + "k" * 30).acting_as("companion", ["*"])


def test_toolkits_become_scopes():
    workflow = "workflow:daily-brief:3:" + "a" * 64
    assert identity_scopes(["notes.read", "conker.remember", workflow]) == [
        "notes.read",
        "automation:daily-brief",
    ]


def test_an_older_toolgate_is_asked_again_later(monkeypatch):
    # One 404 (a ToolGate restarting or being upgraded) must not leave every agent on the bare
    # key until Pi restarts.
    gate, clock = Recorder(put_status=404), [1000.0]
    monkeypatch.setattr(gate_module.httpx, "put", gate.put)
    monkeypatch.setattr(gate_module.time, "monotonic", lambda: clock[0])
    client = ToolGateClient("http://toolgate.test", "tgx_" + "k" * 30)
    assert client.acting_as("companion", ["*"]) is client
    clock[0] += gate_module.IDENTITY_RECHECK_SECONDS - 1
    assert client.acting_as("companion", ["*"]) is client and len(gate.calls) == 1
    gate.put_status, clock[0] = 200, clock[0] + 2
    assert client.acting_as("companion", ["*"]).agent == "companion" and len(gate.calls) == 2
