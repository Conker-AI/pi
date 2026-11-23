"""Web push to the owner's own devices: the server key, the device list, encryption and sending.

Pi has no outbound HTTP (ADR-0005), so the gateway sends: it already holds the owner's browser
state in its private volume, and a push goes only to a device he turned on himself, never to
anyone else. What may be pushed is Pi's decision (`pi/agents/push_queue.py`); the gateway claims
those items and sends each one once.

- The server key (VAPID, RFC 8292) is made on first use and kept next to the TLS key and the
  password verifier (`push-vapid.pem`, 0600). Its private half never leaves this file: not to the
  browser, not to a log, not to an error. The public half goes to the signed-in owner only.
- A device is a browser's push subscription. Only the push services of the browsers he can use
  are accepted as endpoints, so the gateway never posts to an address inside the server's network.
  A device the push service says is gone (404, 410) is removed; sign-out everywhere and a new
  password remove every device (`store.py`).
- A message carries a fixed short title chosen by the item's kind and the Inbox link. Never a
  chat's title, a message, a name or a number from Conker's records: the lock screen is public.
  It is encrypted to the device (RFC 8291, aes128gcm) with `cryptography`, which Pi already pins.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .store import AuthError

LOG = logging.getLogger("gateway.push")
KEY_FILE = "push-vapid.pem"
MAX_DEVICES = 10
EVERY_SECONDS = 30.0
TTL_SECONDS = 6 * 3600  # how long a push service keeps a message for a device that is offline
# RFC 8292 asks for a contact; this names the software, not the owner or his server's address.
SUBJECT = "https://github.com/Conker-AI/conker"
# The push services of Chrome, Edge, Firefox and Safari (and the browsers built on them).
PUSH_HOSTS = (
    "fcm.googleapis.com",
    "android.googleapis.com",
    "push.services.mozilla.com",
    "push.apple.com",
    "notify.windows.com",
)
TITLES = {
    "approval": "Conker needs your approval",
    "suggestion": "Conker has an idea for you",
    "attention": "Something Conker did needs a look",
    "plan": "Your plan usage needs a look",
    "health": "Conker needs a check",
    "update": "A Conker update is ready",
    "update-result": "A Conker update needs a look",
    "change-request": "Conker asks to make something",
    "ask": "Conker has a question for you",
    "agent-job": "An AI job left you a note",
    "memory-recall": "An answer used a memory you changed",
}
GENERIC = "Conker needs you"
INBOX = "/inbox"
_KEY_LOCK = threading.Lock()


class PushUnavailable(Exception):
    """The server key cannot be read or made; notifications stay off."""


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def server_key(directory: Path) -> ec.EllipticCurvePrivateKey:
    """The VAPID key, made once. Two first uses at once still leave exactly one key: each writes a
    complete private file and links it into place, and only the first link succeeds."""
    path = directory / KEY_FILE
    with _KEY_LOCK:
        try:
            if not path.exists():
                key = ec.generate_private_key(ec.SECP256R1())
                pem = key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
                handle, staged = tempfile.mkstemp(prefix=".push-vapid-", dir=directory)
                try:
                    with os.fdopen(handle, "wb") as stream:
                        stream.write(pem)
                        stream.flush()
                        os.fsync(stream.fileno())
                    # Another first use linked its key first: keep that one.
                    with contextlib.suppress(FileExistsError):
                        os.link(staged, path)
                finally:
                    os.unlink(staged)
            if os.name == "posix":
                path.chmod(0o600)
            key = serialization.load_pem_private_key(path.read_bytes(), None)
        except (OSError, ValueError, TypeError):
            raise PushUnavailable from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise PushUnavailable
    return key


def public_key(key: ec.EllipticCurvePrivateKey) -> str:
    return b64(
        key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )


def endpoint_allowed(value: str) -> bool:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (
        len(value) <= 1024
        and parts.scheme == "https"
        and port in (None, 443)
        and not parts.username
        and not parts.password
        and not parts.fragment
        and not any(character in value for character in "\x00\r\n\t ")
        and any(host == name or host.endswith("." + name) for name in PUSH_HOSTS)
    )


def device_keys(p256dh: str, secret: str) -> tuple[bytes, bytes]:
    """A subscription's public key (a P-256 point) and auth secret (16 bytes), checked."""
    point, auth = unb64(p256dh), unb64(secret)
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    if len(point) != 65 or len(auth) != 16:
        raise ValueError("bad subscription keys")
    return point, auth


def encrypt(
    plaintext: bytes,
    point: bytes,
    auth: bytes,
    *,
    salt: bytes | None = None,
    sender: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
    """One aes128gcm record for this device (RFC 8291); a fresh key pair and salt each time."""
    salt = salt or os.urandom(16)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    sender_point = sender.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    shared = sender.exchange(ec.ECDH(), receiver)
    ikm = HKDF(
        hashes.SHA256(), 32, salt=auth, info=b"WebPush: info\x00" + point + sender_point
    ).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    record = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + (4096).to_bytes(4, "big") + bytes([len(sender_point)]) + sender_point
    return header + record


def authorization(key: ec.EllipticCurvePrivateKey, endpoint: str, now: float) -> str:
    """The VAPID header for this endpoint's push service, valid for 12 hours."""
    claims = {"aud": "https://" + (urlsplit(endpoint).hostname or ""), "exp": int(now) + 43200}
    claims["sub"] = SUBJECT
    head = b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64(json.dumps(claims, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    signature = b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{body}.{signature}, k={public_key(key)}"


def message(item: dict) -> bytes:
    """What the lock screen may show: a fixed title for the kind, and where a tap goes."""
    title = TITLES.get(item["kind"], GENERIC)
    value = {"title": title, "url": INBOX, "tag": "conker-" + item["tag"]}
    return json.dumps(value, separators=(",", ":")).encode()


class Devices:
    """The owner's devices with notifications on, in the gateway's own database."""

    def __init__(self, auth):
        self.auth = auth

    def ids(self) -> list[dict]:
        with self.auth.transaction(write=False) as db:
            rows = db.execute("SELECT id,created FROM push_subscriptions ORDER BY created")
            return [{"id": row["id"], "created": row["created"]} for row in rows]

    def add(self, endpoint: str, p256dh: str, secret: str) -> str:
        identity = hashlib.sha256(endpoint.encode()).hexdigest()[:32]
        with self.auth.transaction() as db:
            known = db.execute(
                "SELECT 1 FROM push_subscriptions WHERE id=?", (identity,)
            ).fetchone()
            count = db.execute("SELECT count(*) FROM push_subscriptions").fetchone()[0]
            if not known and count >= MAX_DEVICES:
                raise AuthError(
                    f"Notifications are on for {MAX_DEVICES} devices already. Sign out everywhere"
                    " (Settings, Account) to turn them off on all of them, then turn this one on.",
                    409,
                    code="push_device_limit",
                )
            # The same browser again keeps the time it was first turned on.
            db.execute(
                "INSERT INTO push_subscriptions VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE "
                "SET p256dh=excluded.p256dh, auth=excluded.auth",
                (identity, endpoint, p256dh, secret, self.auth.clock()),
            )
        return identity

    def remove(self, identity: str) -> bool:
        with self.auth.transaction() as db:
            return db.execute("DELETE FROM push_subscriptions WHERE id=?", (identity,)).rowcount > 0

    def every(self) -> list[dict]:
        with self.auth.transaction(write=False) as db:
            return [dict(row) for row in db.execute("SELECT * FROM push_subscriptions")]


def _claimed(response: httpx.Response) -> list[dict]:
    """Pi's claim, strictly: anything else sends nothing."""
    if response.status_code != 200:
        return []
    try:
        items = response.json()["items"]
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(items, list) or len(items) > 20:
        return []
    return [
        {"kind": item["kind"], "urgent": item["urgent"], "tag": item["tag"]}
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("kind"), str)
        and type(item.get("urgent")) is bool
        and isinstance(item.get("tag"), str)
        and re.fullmatch(r"[0-9a-f]{16}", item["tag"])
    ]


class Sender:
    """Claims what Pi lets through and sends it to every device, every `every` seconds."""

    def __init__(self, app, client: httpx.Client, every: float | None = EVERY_SECONDS):
        self.app, self.client, self.every = app, client, every
        self.stop = threading.Event()
        self.thread = None

    def start(self) -> None:
        if self.every:
            self.thread = threading.Thread(target=self._run, name="web-push", daemon=True)
            self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=15)

    def _run(self) -> None:
        while not self.stop.wait(self.every):
            try:
                self.tick()
            except Exception:  # a failed round sends nothing and waits for the next
                LOG.warning("Web push round failed; nothing was sent this round.")

    def tick(self) -> dict:
        config, devices = self.app.state.config, Devices(self.app.state.auth)
        targets = devices.every()
        if not targets or not config.pi_owner_key:
            return {"claimed": 0, "sent": 0, "removed": 0}
        key = server_key(Path(config.database).parent)
        try:
            response = self.app.state.client.post(
                config.pi_url.rstrip("/") + "/push/claim",
                headers={"X-Pi-Owner-Key": config.pi_owner_key},
                json={"after": min(target["created"] for target in targets)},
                timeout=10,
            )
        except httpx.HTTPError:
            return {"claimed": 0, "sent": 0, "removed": 0}
        items, sent, removed, failed = _claimed(response), 0, 0, 0
        for item in items:
            for target in targets:
                if target.get("gone"):
                    continue
                status = self._send(key, target, item)
                if status in (404, 410):
                    devices.remove(target["id"])
                    target["gone"] = True
                    removed += 1
                elif status is not None and 200 <= status < 300:
                    sent += 1
                else:
                    failed += 1
        if failed:
            LOG.warning("Web push: %d of %d sends were not accepted.", failed, failed + sent)
        return {"claimed": len(items), "sent": sent, "removed": removed}

    def _send(self, key, target: dict, item: dict) -> int | None:
        if not endpoint_allowed(target["endpoint"]):
            return None
        try:
            point, auth = device_keys(target["p256dh"], target["auth"])
            body = encrypt(message(item), point, auth)
            response = self.client.post(
                target["endpoint"],
                content=body,
                headers={
                    "TTL": str(TTL_SECONDS),
                    "Urgency": "high" if item["urgent"] else "normal",
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                    "Authorization": authorization(key, target["endpoint"], time.time()),
                },
            )
        except (httpx.HTTPError, ValueError):
            return None
        return response.status_code
