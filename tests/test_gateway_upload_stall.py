"""A browser that stops sending mid-upload is told so (408), never that Pi is down (503)."""

import asyncio

import httpx
import pytest
from starlette.requests import ClientDisconnect

from gateway import relay
from gateway.store import AuthError


class Browser:
    """A request body that sends one chunk, then stalls or goes away."""

    def __init__(self, end):
        self.end = end

    async def stream(self):
        yield b"[1,"
        if self.end == "gone":
            raise ClientDisconnect()
        await asyncio.sleep(60)
        yield b"2]"


async def upstream(request):
    await request.aread()
    return httpx.Response(200, json={"imported": True})


@pytest.mark.parametrize("end", ["stalled", "gone"])
def test_an_upload_the_browser_stops_is_a_408_not_pi_unavailable(monkeypatch, end):
    monkeypatch.setattr(relay, "UPLOAD_READ_SECONDS", 0.05)
    upload = relay.Upload(Browser(end), 1024, AuthError("Too large.", 413))
    send = relay.relay(
        httpx.MockTransport(upstream),
        "POST",
        "http://pi.test/imports/chatgpt",
        {},
        read_seconds=5,
        upload=upload,
    )
    with pytest.raises(AuthError) as refused:
        asyncio.run(send)
    assert (refused.value.status, refused.value.code) == (408, "upload_incomplete")
