import json

import pytest

from pi import filesystem_roots
from pi.system_actions import ActionError
from pi.toolgate import ToolGateClient
from tests.test_system_targets import transport


def catalogue():
    return {
        "mode": "configured",
        "roots": [{"id": "project", "path": "/workspace/project"}],
        "capabilities": {"list": True, "read": False, "write": False},
    }


def test_exact_scoped_transport_and_projection():
    calls = []
    result = filesystem_roots.read(
        ToolGateClient("http://gate.test", "synthetic-key"),
        transport=transport({**catalogue(), "secret": "discard"}, calls),
    )
    assert result == catalogue()
    assert calls[0].url.path == "/v2/agent/system/file-roots" and calls[0].method == "GET"


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "observed"},
        {"roots": [{"id": "project", "path": "/workspace/../outside"}]},
        {"roots": [{"id": "project", "path": "/workspace/secret\nfile"}]},
        {"roots": [{"id": "project", "path": "/safe"}] * 2},
        {"capabilities": {"list": True, "read": True, "write": False}},
        {"capabilities": {"list": 1, "read": 0, "write": 0}},
        {"extra": "x" * filesystem_roots.MAX_ROOT_BYTES},
    ],
)
def test_invalid_catalogue_does_not_enable_files(change):
    with pytest.raises(ActionError, match="unavailable"):
        filesystem_roots.read(
            ToolGateClient("http://gate.test", "key"),
            transport=transport({**catalogue(), **change}),
        )


def test_unavailable_is_distinct_from_empty_directory():
    value = {"mode": "unavailable", "code": "not_configured", "roots": []}
    assert (
        filesystem_roots.read(ToolGateClient("http://gate.test", "key"), transport=transport(value))
        == value
    )
    with pytest.raises(ActionError, match="not configured"):
        filesystem_roots.read(None)


def test_valid_toolgate_unicode_configuration_fits_transport_bound():
    # Within ToolGate's actual character/path/root bounds, even when JSON uses
    # surrogate-pair escapes. No filesystem paths are opened by this test.
    path = "/" + "/".join(["\U0001f332" * 200] * 15)
    configured = {f"root{index}": path for index in range(10)}
    assert len(json.dumps(configured, ensure_ascii=False)) < 32768
    value = {
        **catalogue(),
        "roots": [{"id": key, "path": value} for key, value in configured.items()],
    }
    assert 64000 < len(json.dumps(value).encode()) < filesystem_roots.MAX_ROOT_BYTES
    result = filesystem_roots.read(
        ToolGateClient("http://gate.test", "key"), transport=transport(value)
    )
    assert result == value
