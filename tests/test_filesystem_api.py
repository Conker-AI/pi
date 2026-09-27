from contextlib import closing
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pi import filesystem_api
from pi.browser_contract import owner_allowed, runtime_allowed
from pi.store import Store
from pi.toolgate import ToolGateClient


def test_owner_routes_use_existing_gate_and_do_not_replay_listing(tmp_path, monkeypatch):
    sent = []

    def post(url, **kwargs):
        sent.append((url, kwargs["json"]))
        assert url == "http://gate.test/v2/tools/system.files-list/invoke"
        assert kwargs["json"]["args"] == {"root_id": "project", "path": "docs", "limit": 10}
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "code": "OK",
                "action_id": kwargs["json"]["action_id"],
                "result": {
                    "ok": True,
                    "result": {
                        "mode": "observed",
                        "rootId": "project",
                        "path": "docs",
                        "truncated": True,
                        "sampledAt": datetime.now(UTC).isoformat(),
                        "entries": [
                            {"name": "README.md", "path": "docs/README.md", "kind": "file"}
                        ],
                    },
                },
            },
        )

    monkeypatch.setattr(httpx, "post", post)
    gate = ToolGateClient("http://gate.test", "synthetic-key")
    with closing(Store(tmp_path / "pi.db")) as store:

        def authorize(x_owner: str | None = Header(None)):
            if x_owner != "owner":
                raise HTTPException(401)

        app = FastAPI()
        app.include_router(filesystem_api.router(lambda: store, lambda: gate, authorize))
        with TestClient(app) as client:
            route = "/system/files/listings"
            body = {
                "request_id": "directory_request_01",
                "root_id": "project",
                "path": "docs",
                "limit": 10,
            }
            assert client.post(route, json=body).status_code == 401
            assert client.get("/system/files/roots").status_code == 401
            assert not sent
            headers = {"X-Owner": "owner"}
            first = client.post(route, json=body, headers=headers)
            assert first.status_code == 200 and first.json()["listing"]["truncated"] is True
            assert first.headers["cache-control"] == "no-store"
            assert first.json()["schemaVersion"] == 1
            assert first.json()["authority"] == "none"
            assert first.json()["execution"] == "directory-listing-only"
            assert first.json()["contentIncluded"] is False
            assert first.json()["approvalRequired"] is False
            assert "actionId" not in first.json() and "approval" not in first.json()
            assert "synthetic-key" not in first.text
            assert client.post(route, json=body, headers=headers).json()["state"] == "complete"
            assert len(sent) == 1
            assert (
                client.post(route, json={**body, "path": "../outside"}, headers=headers).status_code
                == 422
            )
            assert (
                client.post(route, json={**body, "path": "other"}, headers=headers).status_code
                == 409
            )
            assert len(sent) == 1


def test_browser_file_routes_are_exact_owner_only_and_reject_queries(tmp_path):
    request_id = "directory_request_01"
    for path in ("/system/files/roots", f"/system/files/listings/{request_id}"):
        assert owner_allowed("GET", path)
        assert not runtime_allowed("GET", path)
    for path in ("/system/files/listings", f"/system/files/listings/{request_id}/resume"):
        assert owner_allowed("POST", path)
        assert not runtime_allowed("POST", path)
    for method, path in (
        ("GET", "/system/files"),
        ("GET", "/system/files/listings/short"),
        ("GET", f"/system/files/listings/{request_id}/content"),
        ("POST", f"/system/files/listings/{request_id}/refresh"),
        ("PUT", "/system/files/listings"),
        ("DELETE", f"/system/files/listings/{request_id}"),
    ):
        assert not owner_allowed(method, path)

    with closing(Store(tmp_path / "pi.db")) as store:
        app = FastAPI()
        app.include_router(filesystem_api.router(lambda: store, lambda: None, lambda: None))
        with TestClient(app) as client:
            response = client.get("/system/files/roots?path=ignored")
            assert response.status_code == 422
            assert response.json()["detail"] == "File routes do not accept query parameters."
