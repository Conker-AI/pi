from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pi.memory import MemoryClient
from pi.memory_explorer_api import router


def test_owner_projection_uses_bound_namespace_and_read_credential():
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"scope": "all", "objects": [], "total": 0})

    memory = MemoryClient(
        "http://memorygate",
        "ingest-private",
        "read-private",
        "companion-bound",
        transport=httpx.MockTransport(upstream),
    )
    app = FastAPI()
    app.include_router(router(lambda: SimpleNamespace(client=memory), lambda: None))
    client = TestClient(app)
    response = client.get("/memory/objects?search=project&limit=10")
    assert response.status_code == 200
    assert seen[0].url.path == "/runtime/library"
    assert seen[0].headers["x-agent-id"] == "companion-bound"
    assert seen[0].headers["x-memorygate-key"] == "read-private"
    assert "x-memorygate-conversation-key" not in seen[0].headers
    assert b'"scope":"all"' in seen[0].content
    assert (
        client.get("/memory/objects/memory/m_1?operation=content&field=text&offset=20").status_code
        == 200
    )
    assert seen[-1].url.path == "/runtime/explore"
    assert client.get("/memory/objects?limit=1000").status_code == 422
    assert client.get("/memory/objects/secret/m_1").status_code == 422
    assert len(seen) == 2
    memory.close()


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"scope": "selected"}, 503),
        (200, [], 503),
        (404, {"detail": "private upstream body"}, 404),
        (500, {"detail": "private upstream body"}, 503),
    ],
)
def test_owner_projection_rejects_bad_scope_and_sanitizes_errors(status, body, expected):
    memory = MemoryClient(
        "http://memorygate",
        "ingest",
        "read",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)),
    )
    app = FastAPI()
    app.include_router(router(lambda: SimpleNamespace(client=memory), lambda: None))
    response = TestClient(app).get("/memory/objects")
    assert response.status_code == expected
    assert "private upstream" not in response.text
    memory.close()


def test_owner_projection_unconfigured_is_not_an_empty_library():
    app = FastAPI()
    app.include_router(router(lambda: SimpleNamespace(client=None), lambda: None))
    assert TestClient(app).get("/memory/objects").status_code == 503
