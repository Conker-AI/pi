"""GET /system/files/roots describes its reply in OpenAPI as the union the dashboard parses (D-01):
one model per mode, chosen by `mode`. The bytes it sends for each mode are the ones Pi sent before
the union (captured on main), and each live reply validates against the union."""

from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi.actions.toolgate import ToolGateClient
from pi.api import server as api
from pi.api.routes import filesystem as filesystem_api
from pi.conversation.store import Store
from pi.core.errors import install
from pi.host import filesystem_reads as reads
from pi.host import filesystem_roots
from tests.test_system_targets import transport

CONFIGURED = {
    "mode": "configured",
    "roots": [{"id": "project", "path": "/workspace/project"}, {"id": "notes", "path": "/n"}],
    "capabilities": {"list": True, "read": False, "write": False},
}
UNAVAILABLE = {"mode": "unavailable", "code": "not_configured", "roots": []}
# What GET /system/files/roots sent on main before the union, byte for byte.
SENT = {
    "configured": (
        b'{"schemaVersion":1,"mode":"configured","code":null,"roots":['
        b'{"id":"project","path":"/workspace/project"},{"id":"notes","path":"/n"}],'
        b'"capabilities":{"list":true,"read":false,"write":false},"authority":"none",'
        b'"execution":"directory-listing-only","contentIncluded":false}'
    ),
    "unavailable": (
        b'{"schemaVersion":1,"mode":"unavailable","code":"not_configured","roots":[],'
        b'"capabilities":null,"authority":"none","execution":"directory-listing-only",'
        b'"contentIncluded":false}'
    ),
}


def reply(tmp_path, monkeypatch, payload):
    read = filesystem_roots.read
    monkeypatch.setattr(
        filesystem_roots, "read", lambda gate: read(gate, transport=transport(payload))
    )
    gate = ToolGateClient("http://gate.test", "synthetic-key")
    with closing(Store(tmp_path / "pi.db")) as store:
        app = FastAPI()
        install(app)
        app.include_router(filesystem_api.router(lambda: store, lambda: gate, lambda: None))
        with TestClient(app) as client:
            return client.get("/system/files/roots")


def test_route_names_the_roots_union_in_openapi():
    document = api.app.openapi()
    route = document["paths"]["/system/files/roots"]["get"]["responses"]["200"]
    assert route["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RootCatalogue"
    }
    union = document["components"]["schemas"]["RootCatalogue"]
    assert union["discriminator"]["propertyName"] == "mode"
    assert union["discriminator"]["mapping"] == {
        "configured": "#/components/schemas/ConfiguredRootCatalogue",
        "unavailable": "#/components/schemas/UnavailableRootCatalogue",
    }


@pytest.mark.parametrize("payload", [CONFIGURED, UNAVAILABLE], ids=["configured", "unavailable"])
def test_each_mode_sends_the_same_bytes_and_matches_the_union(tmp_path, monkeypatch, payload):
    response = reply(tmp_path, monkeypatch, payload)
    assert response.status_code == 200
    assert response.content == SENT[payload["mode"]]
    parsed = reads.RootCatalogue.model_validate_json(response.text).root
    assert parsed.mode == payload["mode"]
    assert parsed.model_dump_json().encode() == response.content


def test_union_refuses_what_the_dashboard_refuses():
    configured = SENT["configured"].decode()
    unavailable = SENT["unavailable"].decode()
    for wrong in (
        configured.replace('"code":null', '"code":"disabled"'),
        configured.replace(
            '"capabilities":{"list":true,"read":false,"write":false}', '"capabilities":null'
        ),
        configured.replace(
            '[{"id":"project","path":"/workspace/project"},{"id":"notes","path":"/n"}]', "[]"
        ),
        unavailable.replace(
            '"capabilities":null', '"capabilities":{"list":true,"read":false,"write":false}'
        ),
        unavailable.replace('"code":"not_configured"', '"code":null'),
        unavailable.replace('"roots":[]', '"roots":[{"id":"project","path":"/p"}]'),
    ):
        with pytest.raises(ValidationError):
            reads.RootCatalogue.model_validate_json(wrong)
