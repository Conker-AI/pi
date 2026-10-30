"""`conker apply requests` retry: a new description, the owner's answer, or neither; never both."""

import io
import json

import httpx
import pytest

from gateway.mutation import MutationError, apply_resource
from pi.api.browser_contract import owner_allowed
from tests.test_gateway_mutation import config

REQUEST = "creq_" + "e" * 32


@pytest.mark.parametrize(
    "fields,body",
    [
        ({}, {}),
        ({"description": "Search Bing"}, {"description": "Search Bing"}),
        ({"answer": "Only English pages"}, {"answer": "Only English pages"}),
    ],
)
def test_a_retry_sends_a_description_an_answer_or_nothing(fields, body):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"id": REQUEST})

    payload = {"operation": "retry", "id": REQUEST, **fields}
    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        apply_resource(config(), "requests", io.BytesIO(json.dumps(payload).encode()), client)
    [request] = seen
    assert request.url.path == f"/change-requests/{REQUEST}/retry"
    assert owner_allowed("POST", request.url.path) and json.loads(request.content) == body


@pytest.mark.parametrize(
    "fields",
    [
        {"description": "Search Bing", "answer": "Only English pages"},
        {"answer": ""},
        {"answer": "x" * 4001},
        {"answer": 7},
    ],
)
def test_a_retry_outside_the_contract_never_reaches_pi(fields):
    def upstream(request):
        raise AssertionError("no request may be sent")

    payload = {"operation": "retry", "id": REQUEST, **fields}
    with (
        httpx.Client(transport=httpx.MockTransport(upstream)) as client,
        pytest.raises(MutationError),
    ):
        apply_resource(config(), "requests", io.BytesIO(json.dumps(payload).encode()), client)
