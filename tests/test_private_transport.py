import httpx
import pytest

from pi.models.private_transport import PrivateTransport


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:18570",
        "http://localhost:18570",
        "https://127.0.0.1:18570",
        "http://user@127.0.0.1:18570",
        "http://127.0.0.1:18570/wrong",
        "http://127.0.0.1:18570?key=x",
    ],
)
def test_private_transport_rejects_nonlocal_or_ambiguous_origin(url):
    with pytest.raises(ValueError):
        PrivateTransport(url, "x" * 32)


def test_fixed_destination_key_and_prefix():
    seen = []
    transport = PrivateTransport(
        "http://127.0.0.1:18570",
        "p" * 32,
        prefix="/inference",
        transport=httpx.MockTransport(
            lambda request: (seen.append(request), httpx.Response(200, json={}))[1]
        ),
    )
    with httpx.Client(transport=transport) as client:
        client.post("http://conker-chatgpt/responses", json={"model": "test"})
        with pytest.raises(ValueError):
            client.get("http://other/status")
    assert str(seen[0].url) == "http://127.0.0.1:18570/inference/responses"
    assert seen[0].headers["authorization"] == "Bearer " + "p" * 32
    assert seen[0].headers["host"] == "127.0.0.1:18570"
