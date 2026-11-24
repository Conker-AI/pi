"""Web push from the gateway: the owner's devices, the server key, the message and the sending."""

import base64
import hashlib
import json
import os
import threading
import time

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from gateway import push
from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore

ORIGIN = "https://localhost:8050"
PASSWORD = "synthetic web push owner phrase"
OWNER_KEY = "o" * 40
FCM = "https://fcm.googleapis.com/fcm/send/device-one"
MOZILLA = "https://updates.push.services.mozilla.com/wpush/v2/device-two"
SECRET = "Plan the surprise for Dana"


def device():
    """A browser's push subscription: its key pair and auth secret."""
    key = ec.generate_private_key(ec.SECP256R1())
    point = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return key, push.b64(point), push.b64(os.urandom(16))


def decrypt(body: bytes, key, p256dh: str, secret: str) -> dict:
    """What the browser does (RFC 8291), written separately from gateway/push.py."""
    salt, size, length = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    sender, record = body[21 : 21 + length], body[21 + length :]
    assert size == 4096 and length == 65
    shared = key.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender)
    )
    info = b"WebPush: info\x00" + push.unb64(p256dh) + sender
    ikm = HKDF(hashes.SHA256(), 32, salt=push.unb64(secret), info=info).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, record, None)
    assert plain.endswith(b"\x02")
    return json.loads(plain[:-1])


class World:
    def __init__(self):
        self.pi, self.pushes = [], []
        self.claim = {"items": []}
        self.answer = None
        self.during_claim = None
        self.status = {FCM: 201, MOZILLA: 201}

    def pi_transport(self, wire):
        self.pi.append(wire)
        if self.during_claim is not None:
            self.during_claim()
        if self.answer is not None:
            return self.answer
        if wire.url.path == "/push/claim":
            return httpx.Response(200, json=self.claim)
        return httpx.Response(200, json={})

    def push_transport(self, wire):
        self.pushes.append(wire)
        return httpx.Response(self.status.get(str(wire.url), 201))


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    state = World()
    config = Config(ORIGIN, str(auth.path), "http://pi", "r" * 32, pi_owner_key=OWNER_KEY)
    app = create_app(
        config,
        store=auth,
        transport=httpx.MockTransport(state.pi_transport),
        push_transport=httpx.MockTransport(state.push_transport),
        push_seconds=None,
    )
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, sign_in(client), state, app, tmp_path


def sign_in(client) -> dict:
    """This browser signs in (each time a new sign-in with its own id); a write's headers."""
    csrf = {"Origin": ORIGIN, "X-CSRF-Token": client.get("/auth/session").json()["csrf_token"]}
    login = client.post("/auth/login", json={"password": PASSWORD}, headers=csrf)
    return {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}


def subscribe(client, headers, endpoint, p256dh, secret, **extra):
    return client.post(
        "/api/push/subscriptions",
        headers=headers,
        json={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": secret}, **extra},
    )


def test_encryption_matches_the_rfc_8291_example():
    sender = ec.derive_private_key(
        int.from_bytes(push.unb64("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
        ec.SECP256R1(),
    )
    body = push.encrypt(
        push.unb64("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
        push.unb64(
            "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
        ),
        push.unb64("BTBZMqHH6r4Tts7J_aSIgg"),
        salt=push.unb64("DGv6ra1nlYgDCS1FRnbzlw"),
        sender=sender,
    )
    assert push.b64(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmY"
        "WAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexS"
        "gSxsj_Qulcy4a-fN"
    )


def test_the_vapid_header_is_a_valid_es256_token_for_the_push_service(tmp_path):
    key = push.server_key(tmp_path)
    header = push.authorization(key, FCM, 1_800_000_000)
    token, public = header.removeprefix("vapid t=").split(", k=")
    head, claims, signature = token.split(".")
    assert json.loads(push.unb64(head)) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(push.unb64(claims)) == {
        "aud": "https://fcm.googleapis.com",
        "exp": 1_800_043_200,
        "sub": push.SUBJECT,
    }
    raw = push.unb64(signature)
    verifier = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push.unb64(public))
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    verifier.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    with pytest.raises(InvalidSignature):
        verifier.verify(der, f"{head}.{claims}x".encode(), ec.ECDSA(hashes.SHA256()))


def test_the_server_key_is_made_once_private_and_only_its_public_half_is_served(world):
    client, _, _, _, folder = world
    first, second = client.get("/api/push").json(), client.get("/api/push").json()
    assert first["publicKey"] == second["publicKey"] and first["devices"] == []
    path = folder / push.KEY_FILE
    pem = path.read_bytes()
    assert b"PRIVATE KEY" in pem and push.public_key(push.server_key(folder)) == first["publicKey"]
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    loaded = serialization.load_pem_private_key(pem, None)
    assert isinstance(loaded, ec.EllipticCurvePrivateKey)
    private = loaded.private_numbers().private_value
    assert push.b64(private.to_bytes(32, "big")) not in json.dumps(first)
    assert [name for name in os.listdir(folder) if name.startswith(".push-vapid-")] == []


def test_two_first_uses_at_once_leave_one_key(tmp_path):
    keys, barrier = [], threading.Barrier(6)

    def make():
        barrier.wait()
        keys.append(push.public_key(push.server_key(tmp_path)))

    threads = [threading.Thread(target=make) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(keys)) == 1


def test_an_unreadable_key_fails_closed(world):
    client, headers, state, app, folder = world
    _, p256dh, secret = device()
    assert subscribe(client, headers, FCM, p256dh, secret).status_code == 200
    (folder / push.KEY_FILE).write_bytes(b"not a key")
    answer = client.get("/api/push")
    assert answer.status_code == 503 and "not a key" not in answer.text
    state.claim = {"items": [{"kind": "approval", "urgent": False, "tag": "a" * 16}]}
    with pytest.raises(push.PushUnavailable):
        app.state.push.tick()
    # Nothing was claimed, so nothing was lost, and nothing was sent.
    assert state.pi == [] and state.pushes == []


def test_only_the_signed_in_owner_reaches_the_routes(world):
    client, headers, _, _, _ = world
    _, p256dh, secret = device()
    body = {"endpoint": FCM, "keys": {"p256dh": p256dh, "auth": secret}}
    # Another browser, never signed in (same app; its lifespan already runs).
    stranger = TestClient(client.app, base_url=ORIGIN)
    assert stranger.get("/api/push").status_code == 401
    csrf = stranger.get("/auth/session").json()["csrf_token"]
    anonymous = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    assert stranger.post("/api/push/subscriptions", headers=anonymous, json=body).status_code == 401
    assert client.post("/api/push/subscriptions", json=body).status_code == 403
    other = {**headers, "Origin": "https://elsewhere.example"}
    assert client.post("/api/push/subscriptions", headers=other, json=body).status_code == 403
    assert client.get("/api/push?x=1").status_code == 422
    query = client.post("/api/push/subscriptions?x=1", headers=headers, json=body)
    assert query.status_code == 422
    assert client.get("/api/push").json()["devices"] == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://pi:8050/push/claim",
        "http://fcm.googleapis.com/fcm/send/x",
        "https://fcm.googleapis.com.example.net/x",
        "https://user@fcm.googleapis.com/x",
        "https://fcm.googleapis.com:8443/x",
        "https://fcm.googleapis.com/x#y",
        "https://127.0.0.1/x",
        "https://localhost/x",
    ],
)
def test_only_a_browser_push_service_is_an_endpoint(world, endpoint):
    client, headers, _, _, _ = world
    _, p256dh, secret = device()
    answer = subscribe(client, headers, endpoint, p256dh, secret)
    assert answer.status_code == 422
    assert endpoint not in answer.text and p256dh not in answer.text


def test_malformed_keys_and_extra_fields_are_refused(world):
    client, headers, _, _, _ = world
    _, p256dh, secret = device()
    for body in (
        {"endpoint": FCM, "keys": {"p256dh": p256dh}},
        {"endpoint": FCM, "keys": {"p256dh": p256dh, "auth": "short"}},
        {"endpoint": FCM, "keys": {"p256dh": "BAAA", "auth": secret}},
        {"endpoint": FCM, "keys": {"p256dh": p256dh, "auth": secret}, "expirationTime": None},
        {"endpoint": [FCM], "keys": {"p256dh": p256dh, "auth": secret}},
    ):
        answer = client.post("/api/push/subscriptions", headers=headers, json=body)
        assert answer.status_code == 422 and secret not in answer.text


def test_a_device_is_turned_on_listed_and_turned_off(world):
    client, headers, _, _, _ = world
    _, p256dh, secret = device()
    on = subscribe(client, headers, FCM, p256dh, secret).json()
    identity = hashlib.sha256(FCM.encode()).hexdigest()[:32]
    assert on["device"] == identity and on["limit"] == push.MAX_DEVICES
    assert [item["id"] for item in on["devices"]] == [identity]
    again = subscribe(client, headers, FCM, p256dh, secret).json()["devices"]
    assert again == on["devices"]  # the same browser again keeps its first time
    assert client.get("/api/push").json()["devices"] == on["devices"]
    off = client.post(f"/api/push/subscriptions/{identity}/delete", headers=headers, json={})
    assert off.json() == {"removed": True, "devices": [], "limit": push.MAX_DEVICES}
    again = client.post(f"/api/push/subscriptions/{identity}/delete", headers=headers, json={})
    assert again.json()["removed"] is False
    bad = client.post("/api/push/subscriptions/NOT-A-DEVICE/delete", headers=headers, json={})
    assert bad.status_code == 422
    body = client.post(f"/api/push/subscriptions/{identity}/delete", headers=headers, json={"a": 1})
    assert body.status_code == 422


def test_at_most_ten_devices(world):
    client, headers, _, _, _ = world
    for number in range(push.MAX_DEVICES):
        _, p256dh, secret = device()
        assert subscribe(client, headers, f"{FCM}-{number}", p256dh, secret).status_code == 200
    _, p256dh, secret = device()
    refused = subscribe(client, headers, f"{FCM}-more", p256dh, secret)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "push_device_limit"


def test_sign_out_everywhere_and_a_new_password_turn_every_device_off(world):
    client, headers, _, app, _ = world
    _, p256dh, secret = device()
    subscribe(client, headers, FCM, p256dh, secret)
    app.state.auth.revoke()
    assert push.Devices(app.state.auth).ids() == []
    subscribe(client, sign_in(client), FCM, p256dh, secret)
    assert len(push.Devices(app.state.auth).ids()) == 1
    app.state.auth.set_password("another synthetic owner phrase")
    assert push.Devices(app.state.auth).ids() == []


def test_a_device_is_never_put_on_a_sign_in_that_was_signed_out(world):
    _, _, _, app, _ = world
    _, p256dh, secret = device()
    gone = app.state.auth.sessions()[0]["id"]
    app.state.auth.revoke(gone)  # sign-out lands while the request waits for its body
    for renew in (False, True):
        with pytest.raises(auth_module.AuthError) as refused:
            push.Devices(app.state.auth).add(
                FCM, p256dh, secret, session=gone, key="k", label="Chrome", renew=renew
            )
        assert refused.value.status == 401
    assert push.Devices(app.state.auth).ids() == []


def test_a_round_sends_fixed_words_to_every_device_and_drops_a_gone_one(world):
    client, headers, state, app, _ = world
    one, two = device(), device()
    subscribe(client, headers, FCM, one[1], one[2])
    subscribe(client, headers, MOZILLA, two[1], two[2])
    state.status[MOZILLA] = 410
    app.state.push.notices.clear()  # the "one more device" notice has its own test
    # Pi sends no words; even if it did, they would not reach the lock screen.
    state.claim = {
        "items": [{"kind": "approval", "urgent": True, "tag": "0123456789abcdef", "title": SECRET}]
    }
    result = app.state.push.tick()
    assert result == {"claimed": 1, "sent": 1, "removed": 1}
    claim = json.loads(state.pi[-1].content)
    assert state.pi[-1].headers["x-pi-owner-key"] == OWNER_KEY and set(claim) == {"after"}
    sent = {str(wire.url): wire for wire in state.pushes}
    wire = sent[FCM]
    assert wire.headers["content-encoding"] == "aes128gcm"
    assert wire.headers["urgency"] == "high" and wire.headers["ttl"] == str(push.TTL_SECONDS)
    assert wire.headers["authorization"].startswith("vapid t=") and "cookie" not in wire.headers
    assert SECRET.encode() not in wire.content
    message = decrypt(wire.content, one[0], one[1], one[2])
    assert message == {
        "title": push.TITLES["approval"],
        "url": "/inbox",
        "tag": "conker-0123456789abcdef",
    }
    listed = client.get("/api/push").json()["devices"]
    assert [item["id"] for item in listed] == [hashlib.sha256(FCM.encode()).hexdigest()[:32]]
    # The gone device is not tried again.
    state.pushes.clear()
    app.state.push.tick()
    assert [str(wire.url) for wire in state.pushes] == [FCM]


def test_an_unknown_kind_gets_the_plain_title():
    message = json.loads(push.message({"kind": "new-thing", "urgent": False, "tag": "f" * 16}))
    assert message["title"] == push.GENERIC


def test_no_device_means_no_claim(world):
    _, _, state, app, _ = world
    assert app.state.push.tick() == {"claimed": 0, "sent": 0, "removed": 0}
    assert state.pi == []


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(
            500, json={"items": [{"kind": "approval", "urgent": False, "tag": "a" * 16}]}
        ),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"items": "x"}),
        httpx.Response(200, json={"items": [{"kind": "approval", "urgent": "yes", "tag": "a"}]}),
    ],
)
def test_an_unreadable_claim_sends_nothing(world, answer):
    client, headers, state, app, _ = world
    _, p256dh, secret = device()
    subscribe(client, headers, FCM, p256dh, secret)
    state.answer = answer
    assert app.state.push.tick()["sent"] == 0 and state.pushes == []


def test_a_stored_endpoint_outside_the_push_services_is_never_posted(world):
    _, _, state, app, folder = world
    _, p256dh, secret = device()
    current = push.public_key_id(push.server_key(folder))
    # A well-formed host on 443 and the current key, so only the push-service allowlist refuses it.
    endpoint = "https://169.254.169.254/latest/meta-data"
    with app.state.auth.transaction() as db:
        db.execute(
            "INSERT INTO push_subscriptions VALUES(?,?,?,?,?,?,?,?,0)",
            ("b" * 32, endpoint, p256dh, secret, 1.0, "s", current, "Chrome"),
        )
    state.claim = {"items": [{"kind": "ask", "urgent": False, "tag": "c" * 16}]}
    assert app.state.push.tick()["sent"] == 0 and state.pushes == []


def test_the_browser_cannot_make_a_claim_through_the_control_relay(world):
    client, headers, state, _, _ = world
    answer = client.post("/api/control/pi/push/claim", headers=headers, json={"after": 0})
    assert answer.status_code == 403 and state.pi == []


def test_public_key_is_base64url_p256_point(world):
    client, _, _, _, _ = world
    raw = base64.urlsafe_b64decode(client.get("/api/push").json()["publicKey"] + "=")
    assert len(raw) == 65 and raw[0] == 4


@pytest.mark.parametrize(
    "endpoint",
    [
        r"https://evil.example\.fcm.googleapis.com/x",
        "https://evil.example;.push.apple.com/x",
        "https://evil.example%2f.push.apple.com/x",
        "https://evil_example.notify.windows.com/x",
    ],
)
def test_a_push_host_is_plain_dns_letters_only(endpoint):
    # httpx would send these hosts to the resolver as written; only a plain DNS name is a push host.
    assert push.endpoint_allowed(endpoint) is False


def test_sign_out_everywhere_during_a_claim_sends_nothing(world):
    client, headers, state, app, _ = world
    _, p256dh, secret = device()
    subscribe(client, headers, FCM, p256dh, secret)
    state.claim = {"items": [{"kind": "approval", "urgent": False, "tag": "d" * 16}]}
    # The owner signs out everywhere while Pi answers the claim: the devices read before it are
    # no longer the owner's, so none of them is woken.
    state.during_claim = app.state.auth.revoke
    assert app.state.push.tick()["sent"] == 0 and state.pushes == []


ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36"
)
WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"


def test_devices_are_listed_newest_first_with_a_short_browser_name(world):
    client, headers, _, app, _ = world
    one, two = device(), device()
    subscribe(client, {**headers, "User-Agent": ANDROID}, FCM, one[1], one[2])
    subscribe(client, {**headers, "User-Agent": WINDOWS}, MOZILLA, two[1], two[2])
    listed = client.get("/api/push").json()["devices"]
    ids = [hashlib.sha256(url.encode()).hexdigest()[:32] for url in (MOZILLA, FCM)]
    assert [item["id"] for item in listed] == ids
    assert [item["label"] for item in listed] == ["Firefox on Windows", "Chrome on Android"]
    assert listed[0]["created"] >= listed[1]["created"] > 0
    with app.state.auth.transaction() as db:
        assert "Mozilla/5.0" not in json.dumps(
            [dict(row) for row in db.execute("SELECT * FROM push_subscriptions")]
        )


def test_signing_out_the_browser_that_turned_a_device_on_turns_it_off(world):
    client, headers, _, app, _ = world
    _, p256dh, secret = device()
    subscribe(client, headers, FCM, p256dh, secret)
    app.state.auth.revoke("another-browser-session-id")
    assert len(push.Devices(app.state.auth).ids()) == 1
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert push.Devices(app.state.auth).ids() == []


def test_a_sign_in_that_idles_out_keeps_its_device_on(world):
    client, headers, _, app, _ = world
    _, p256dh, secret = device()
    subscribe(client, headers, FCM, p256dh, secret)
    later = time.time() + 2 * 86400
    app.state.auth.clock = lambda: later
    assert app.state.auth.sessions() == []  # the sign-in has expired
    assert len(push.Devices(app.state.auth).ids()) == 1  # notifications are for a closed tab


def test_the_limit_says_to_remove_a_device_in_settings(world):
    client, headers, _, _, _ = world
    for number in range(push.MAX_DEVICES):
        _, p256dh, secret = device()
        subscribe(client, headers, f"{FCM}-{number}", p256dh, secret)
    _, p256dh, secret = device()
    refused = subscribe(client, headers, f"{FCM}-more", p256dh, secret).json()["detail"]
    assert "Remove one" in refused["message"] and "Settings" in refused["message"]


@pytest.mark.parametrize("status", [401, 403])
def test_a_device_the_push_service_refuses_two_rounds_in_a_row_is_removed(world, status):
    client, headers, state, app, _ = world
    _, p256dh, secret = device()
    subscribe(client, headers, FCM, p256dh, secret)
    state.status[FCM] = status
    items = [{"kind": "ask", "urgent": False, "tag": tag * 16} for tag in "abc"]
    state.claim = {"items": items}
    # One refusal can be the push service's own bad moment: the device stays, and a round with
    # three items asks it once.
    assert app.state.push.tick()["removed"] == 0 and len(state.pushes) == 1
    assert len(push.Devices(app.state.auth).ids()) == 1
    state.status[FCM] = 201  # it takes a message again: the count starts over
    assert app.state.push.tick()["sent"] == 3
    state.status[FCM] = status
    assert app.state.push.tick()["removed"] == 0
    assert app.state.push.tick()["removed"] == 1  # the second round in a row
    assert push.Devices(app.state.auth).ids() == []


def test_a_new_server_key_drops_the_devices_turned_on_with_the_old_one(world):
    client, headers, state, app, folder = world
    _, p256dh, secret = device()
    subscribe(client, headers, FCM, p256dh, secret)
    (folder / push.KEY_FILE).unlink()
    app.state.push.start()  # a restart: the key the device was turned on with is gone
    assert push.Devices(app.state.auth).ids() == []
    subscribe(client, headers, FCM, p256dh, secret)
    (folder / push.KEY_FILE).unlink()
    client.get("/api/push")  # a new key while running
    state.claim = {"items": [{"kind": "ask", "urgent": False, "tag": "e" * 16}]}
    assert app.state.push.tick()["sent"] == 0 and state.pushes == []
    assert push.Devices(app.state.auth).ids() == []


def test_the_other_devices_hear_when_one_more_is_turned_on(world):
    client, headers, state, app, _ = world
    one, two = device(), device()
    subscribe(client, {**headers, "User-Agent": ANDROID}, FCM, one[1], one[2])
    assert app.state.push.tick()["sent"] == 0  # the first device has no one to tell
    subscribe(client, {**headers, "User-Agent": WINDOWS}, MOZILLA, two[1], two[2])
    subscribe(client, {**headers, "User-Agent": WINDOWS}, MOZILLA, two[1], two[2])  # again: no news
    app.state.push.tick()
    assert [str(wire.url) for wire in state.pushes] == [FCM]
    told = decrypt(state.pushes[0].content, one[0], one[1], one[2])
    assert told["title"] == "A new device will get Conker notifications: Firefox on Windows"
    assert told["url"] == "/settings?group=notifications" and len(told["title"]) <= 80


@pytest.mark.parametrize("renew", [False, True])
def test_the_same_browser_sent_after_a_new_sign_in_moves_to_that_sign_in(world, renew):
    client, headers, _, app, _ = world
    _, p256dh, secret = device()
    subscribe(client, headers, FCM, p256dh, secret)
    told = list(app.state.push.notices)
    later = time.time() + 3600  # the first sign-in idled out; the same browser signs in again
    app.state.auth.clock = lambda: later
    again = sign_in(client)
    extra = {"renew": True} if renew else {}
    assert subscribe(client, again, FCM, p256dh, secret, **extra).status_code == 200
    assert app.state.push.notices == told  # the same device again is no news
    assert client.post("/auth/logout", headers=again).status_code == 200
    assert push.Devices(app.state.auth).ids() == []  # signing out here turns it off


def test_a_renewal_never_turns_a_removed_device_back_on(world):
    client, headers, _, app, _ = world
    _, p256dh, secret = device()
    on = subscribe(client, headers, FCM, p256dh, secret).json()["device"]
    client.post(f"/api/push/subscriptions/{on}/delete", headers=headers, json={})
    renewed = subscribe(client, headers, FCM, p256dh, secret, renew=True)
    assert renewed.status_code == 404 and renewed.json()["detail"]["code"] == "push_device_off"
    assert secret not in renewed.text and FCM not in renewed.text
    assert push.Devices(app.state.auth).ids() == []
    for value in ("yes", 1, None):
        assert subscribe(client, headers, FCM, p256dh, secret, renew=value).status_code == 422
