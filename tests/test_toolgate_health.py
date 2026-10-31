"""Pi's view of ToolGate's status never crashes on what ToolGate answers (pi/actions/toolgate.py)."""

import httpx
import pytest

from pi.actions import toolgate as gate_module
from pi.actions.toolgate import ToolGateClient


def answer(monkeypatch, response):
    def get(url, **kwargs):
        return response(httpx.Request("GET", url))

    monkeypatch.setattr(gate_module.httpx, "get", get)
    return ToolGateClient("http://toolgate.test", "tgx_" + "k" * 30).health()


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"lockdown": False}, {"status": "ok"}),
        ({"lockdown": True}, {"status": "degraded", "reason": "lockdown"}),
        (["not", "an", "object"], {"status": "unavailable", "reason": "unreadable status"}),
    ],
)
def test_status_reads_the_json_body(monkeypatch, body, status):
    assert answer(monkeypatch, lambda request: httpx.Response(200, json=body, request=request)) == (
        status
    )


def test_a_body_that_is_not_json_is_unreadable_not_a_crash(monkeypatch):
    def truncated(request):
        return httpx.Response(200, content=b'{"lockdown": tr', request=request)

    assert answer(monkeypatch, truncated) == {
        "status": "unavailable",
        "reason": "unreadable status",
    }
