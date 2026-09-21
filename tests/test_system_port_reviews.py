import json
import time

import httpx
import pytest

from pi import system_port_reviews as reviews
from pi.system_actions import ActionError
from pi.toolgate import ToolGateClient

CID, RID = "a" * 64, "b" * 48
MAPPING = {"hostAddress": "127.0.0.1", "hostPort": 8080, "containerPort": 80, "protocol": "tcp"}
BODY = reviews.Request(container_id=CID, operation="create", mapping=MAPPING)


def response():
    return {
        "reviewId": RID,
        "expiresAt": time.time() + 300,
        "consumed": False,
        "expired": False,
        "private": "never-show",
        "preview": {
            "containerId": CID,
            "operation": "create",
            "execution": "owner_approval_required",
            "hostAvailability": "not_checked",
            "bindingSource": "observed",
            "writableLayer": "snapshot_required",
            "volumeData": "reuse_existing",
            "tmpfsData": "none",
            "changed": True,
            "requiresReplacement": True,
            "downtimeExpected": True,
            "before": [],
            "after": [{**MAPPING, "debug": "never-show"}],
        },
    }


class Stream(httpx.SyncByteStream):
    def __init__(self, data):
        self.data = data

    def __iter__(self):
        yield self.data


def gate():
    return ToolGateClient("http://synthetic-gate", "synthetic-key")


def test_exact_create_and_get_paths_auth_and_projection():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, stream=Stream(json.dumps(response()).encode()))

    transport = httpx.MockTransport(handle)
    created = reviews.fetch(gate(), request=BODY, transport=transport)
    assert created["reviewId"] == RID and "never-show" not in json.dumps(created)
    assert seen[0].method == "POST" and seen[0].url.path == "/v2/agent/system/port-reviews"
    assert json.loads(seen[0].content) == BODY.model_dump(exclude_none=True)
    assert seen[0].headers["X-ToolGate-Execution-Key"] == "synthetic-key"
    reviews.fetch(gate(), review_id=RID, transport=transport)
    assert seen[-1].method == "GET" and seen[-1].url.path.endswith("/" + RID)


@pytest.mark.parametrize(
    "change", ["container", "mapping", "flags", "duplicate", "review", "expired"]
)
def test_wrong_review_never_presented_as_requested_change(change):
    value = response()
    if change == "container":
        value["preview"]["containerId"] = "c" * 64
    elif change == "mapping":
        value["preview"]["after"][0]["hostPort"] = 9090
    elif change == "flags":
        value["preview"]["changed"] = False
    elif change == "duplicate":
        value["preview"]["after"].append(value["preview"]["after"][0])
    elif change == "review":
        value["reviewId"] = "bad"
    else:
        value["expired"] = True
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Stream(json.dumps(value).encode()))
    )
    with pytest.raises(ActionError) as error:
        reviews.fetch(gate(), request=BODY, transport=transport)
    assert "never-show" not in str(error.value)


@pytest.mark.parametrize("failure", ["redirect", "oversize", "duplicate-json", "timeout"])
def test_bounded_transport_no_retry_and_static_error(failure):
    calls = []

    def handle(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("never-show")
        data = b"x" * (256 * 1024 + 1) if failure == "oversize" else b'{"x":1,"x":2}'
        return httpx.Response(307 if failure == "redirect" else 200, stream=Stream(data))

    with pytest.raises(ActionError) as error:
        reviews.fetch(gate(), request=BODY, transport=httpx.MockTransport(handle))
    assert len(calls) == 1 and "never-show" not in str(error.value)


def test_invalid_create_does_not_reach_transport():
    with pytest.raises(ValueError):
        reviews.Request(container_id=CID, operation="remove", mapping=MAPPING)
    with pytest.raises(ValueError):
        reviews.Request(
            container_id=CID, operation="create", mapping={**MAPPING, "hostAddress": "example.com"}
        )
