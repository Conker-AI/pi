"""`conker apply`: a write that may have been made says so (hunt pi-gateway-1 F7)."""

import io
import json

import httpx
import pytest

from gateway.mutation import MutationError, apply_resource
from tests.test_gateway_mutation import config

PAYLOAD = {"expected_revision": 4, "configuration": {"models": []}}


def apply(upstream):
    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        return apply_resource(config(), "models", io.BytesIO(json.dumps(PAYLOAD).encode()), client)


def raises(error):
    def upstream(request):
        raise error("synthetic", request=request)

    return upstream


@pytest.mark.parametrize(
    "upstream",
    [
        raises(httpx.ReadTimeout),
        raises(httpx.RemoteProtocolError),
        lambda request: httpx.Response(
            200, text="not json", headers={"content-type": "text/plain"}
        ),
        lambda request: httpx.Response(200, json=["not", "an", "object"]),
    ],
)
def test_an_answer_lost_after_sending_may_have_been_made(upstream):
    with pytest.raises(MutationError, match="may already have been made"):
        apply(upstream)


def test_a_service_never_reached_wrote_nothing():
    with pytest.raises(MutationError) as refused:
        apply(raises(httpx.ConnectError))
    assert "unavailable" in str(refused.value) and "may already" not in str(refused.value)
