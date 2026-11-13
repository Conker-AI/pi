"""The owner search routes describe their replies in OpenAPI (D-01), and what they send matches
the description: every search flow in test_universal_search runs again, and each 200 reply of a
/search route is validated against the model its route names."""

import inspect

import pytest
import test_universal_search as flows
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.context import search_replies
from pi.context import universal_search as search

MODELS = {
    "/search": search_replies.SearchPage,
    "/search/capabilities": search_replies.SearchCapabilities,
    "/search/settings": search.SearchSettingsDocument,
}
FLOWS = [
    name
    for name, test in vars(flows).items()
    if name.startswith("test_") and "tmp_path" in inspect.signature(test).parameters
]


class Checked(TestClient):
    seen: list[str] = []

    def request(self, method, url, *args, **kwargs):
        reply = super().request(method, url, *args, **kwargs)
        path = str(url).split("?")[0]
        if path in MODELS and reply.status_code == 200:
            MODELS[path].model_validate_json(reply.text)
            type(self).seen.append(path)
        return reply


def run(name, tmp_path, monkeypatch):
    test = getattr(flows, name)
    params = inspect.signature(test).parameters
    test(tmp_path=tmp_path, **({"monkeypatch": monkeypatch} if "monkeypatch" in params else {}))


@pytest.mark.parametrize("path", MODELS)
def test_each_route_names_its_reply_model_in_openapi(path):
    paths = api.app.openapi()["paths"][path]
    for method in paths:
        schema = paths[method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{MODELS[path].__name__}"}


def test_every_search_reply_in_the_search_flows_matches_its_model(tmp_path, monkeypatch):
    monkeypatch.setattr(flows, "TestClient", Checked)
    Checked.seen = []
    for name in FLOWS:
        (tmp_path / name).mkdir()
        run(name, tmp_path / name, monkeypatch)
    assert {"/search", "/search/settings"} <= set(Checked.seen)


def test_capabilities_match_their_model(tmp_path):
    from contextlib import closing

    from pi.conversation.store import Store

    with closing(Store(tmp_path / "pi.db")) as store:
        reply = flows.client_for(store).get("/search/capabilities")
        search_replies.SearchCapabilities.model_validate_json(reply.text)
