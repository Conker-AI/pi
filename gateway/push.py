"""Web push to the owner's own devices: the server key, the device list, encryption and sending.

Pi has no outbound HTTP (ADR-0005), so the gateway sends: it already holds the owner's browser
state in its private volume, and a push goes only to a device the owner turned on, never to
anyone else. What may be pushed is Pi's decision (`pi/agents/push_queue.py`); the gateway claims
those items and sends each one once.

- The server key (VAPID, RFC 8292) is made on first use and kept next to the TLS key and the
  password verifier (`push-vapid.pem`, 0600). Its private half never leaves this file: not to the
  browser, not to a log, not to an error. The public half goes to the signed-in owner only.
- A device is a browser's push subscription. Only the push services of the browsers the owner
  can use are accepted as endpoints, so the gateway never posts to an address inside the server's
  network. A device the push service refuses or calls gone (401, 403, 404, 410) is removed, and
  so is one turned on with a server key that is no longer the key. Signing out the browser that
  turned it on removes it; sign-out everywhere and a new password remove every device
  (`store.py`). A sign-in that only idles out keeps its device: notifications are for a closed
  tab. When one more device is turned on, the others are told, so a stranger's is noticed.
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
# RFC 8292 asks for a contact; this names the software, not the owner or the owner's server address.
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
DNS_NAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
GENERIC = "Conker needs you"
INBOX = "/inbox"
SETTINGS = "/settings?group=notifications"
GONE = (404, 410)  # the push service no longer takes messages for this device
# It refuses this server for the device. Once can be the push service's own bad moment, so a device
# goes after two rounds in a row (a device of an old key never reaches a round: `keep_key`).
REFUSED = (401, 403)
# A device's short name, from fixed words only: the User-Agent itself is never kept.
BROWSERS = (
    ("Edg", "Edge"),
    ("OPR/", "Opera"),
    ("SamsungBrowser/", "Samsung Internet"),
    ("Firefox/", "Firefox"),
    ("FxiOS/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
SYSTEMS = (
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Android", "Android"),
    ("Windows", "Windows"),
    ("CrOS", "ChromeOS"),
    ("Macintosh", "Mac"),
    ("Linux", "Linux"),
)
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
        # A plain DNS name: httpx hands any other host characters to the resolver as written.
        and DNS_NAME.fullmatch(host) is not None
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


def public_key_id(key: ec.EllipticCurvePrivateKey) -> str:
    """Which server key a device was turned on with; its subscription works with that key only."""
    return hashlib.sha256(public_key(key).encode()).hexdigest()[:16]


def device_label(agent: str) -> str:
    """A browser's short name from fixed words, such as "Chrome on Android"."""
    browser = next((name for mark, name in BROWSERS if mark in agent), "A browser")
    system = next((name for mark, name in SYSTEMS if mark in agent), "")
    return f"{browser} on {system}" if system else browser


def _payload(title: str, url: str, tag: str) -> bytes:
    value = {"title": title, "url": url, "tag": "conker-" + tag}
    return json.dumps(value, separators=(",", ":")).encode()


def message(item: dict) -> bytes:
    """What the lock screen may show: a fixed title for the kind, and where a tap goes."""
    return _payload(TITLES.get(item["kind"], GENERIC), INBOX, item["tag"])


def notice(device: str, label: str) -> bytes:
    """Tells the other devices that one more gets notifications now, and where to remove it."""
    return _payload(f"A new device will get Conker notifications: {label}", SETTINGS, device[:16])


class Devices:
    """The owner's devices with notifications on, in the gateway's own database."""

    def __init__(self, auth):
        self.auth = auth

    def ids(self) -> list[dict]:
        """Newest first: each device's id, when it was turned on and its browser's short name."""
        with self.auth.transaction(write=False) as db:
            rows = db.execute(
                "SELECT id,created,label FROM push_subscriptions ORDER BY created DESC, id"
            )
            return [dict(row) for row in rows]

    def add(
        self,
        endpoint: str,
        p256dh: str,
        secret: str,
        *,
        session: str,
        key: str,
        label: str,
        renew: bool = False,
    ) -> tuple[str, bool]:
        """Turn a device on for this sign-in and server key; also says whether it is new. `renew`
        only moves a device that is on to this sign-in: it never turns one on again."""
        identity = hashlib.sha256(endpoint.encode()).hexdigest()[:32]
        with self.auth.transaction() as db:
            known = db.execute(
                "SELECT 1 FROM push_subscriptions WHERE id=?", (identity,)
            ).fetchone()
            if renew and not known:
                raise AuthError(
                    "Notifications are off for this device. Turn them on in Settings.",
                    404,
                    code="push_device_off",
                )
            count = db.execute("SELECT count(*) FROM push_subscriptions").fetchone()[0]
            if not known and count >= MAX_DEVICES:
                raise AuthError(
                    f"Notifications are on for {MAX_DEVICES} devices already. Remove one in"
                    " Settings, Notifications, then turn this one on.",
                    409,
                    code="push_device_limit",
                )
            # The same browser again keeps the time it was first turned on.
            db.execute(
                "INSERT INTO push_subscriptions(id,endpoint,p256dh,auth,created,session_id,key_id,"
                "label) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "p256dh=excluded.p256dh, auth=excluded.auth, session_id=excluded.session_id, "
                "key_id=excluded.key_id, label=excluded.label, refused=0",
                (identity, endpoint, p256dh, secret, self.auth.clock(), session, key, label),
            )
        return identity, not known

    def remove(self, identity: str) -> bool:
        with self.auth.transaction() as db:
            return db.execute("DELETE FROM push_subscriptions WHERE id=?", (identity,)).rowcount > 0

    def keep_key(self, current: str) -> None:
        """Drop the devices turned on with another server key: their push service refuses it."""
        with self.auth.transaction() as db:
            db.execute("DELETE FROM push_subscriptions WHERE key_id<>?", (current,))

    def every(self) -> list[dict]:
        with self.auth.transaction(write=False) as db:
            return [dict(row) for row in db.execute("SELECT * FROM push_subscriptions")]

    def counted(self, refused: set[str], accepted: set[str]) -> int:
        """After a round: a device that took a message starts over; one the push service refused
        (401/403) is removed when that is the second round in a row. Says how many were removed."""
        removed = 0
        with self.auth.transaction() as db:
            for identity in accepted:
                db.execute("UPDATE push_subscriptions SET refused=0 WHERE id=?", (identity,))
            for identity in refused - accepted:
                db.execute(
                    "UPDATE push_subscriptions SET refused=refused+1 WHERE id=?", (identity,)
                )
                removed += db.execute(
                    "DELETE FROM push_subscriptions WHERE id=? AND refused>=2", (identity,)
                ).rowcount
        return removed


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
        self.lock = threading.Lock()
        self.notices: list[tuple[str, str]] = []  # (new device, its short name), until sent

    def start(self) -> None:
        # A device turned on with a key that is gone or replaced can never be reached again.
        folder = Path(self.app.state.config.database).parent
        with contextlib.suppress(PushUnavailable):  # unreadable: every round fails closed
            current = (folder / KEY_FILE).exists() and public_key_id(server_key(folder))
            Devices(self.app.state.auth).keep_key(current or "")
        if self.every:
            self.thread = threading.Thread(target=self._run, name="web-push", daemon=True)
            self.thread.start()

    def announce(self, device: str, label: str) -> None:
        with self.lock:
            self.notices.append((device, label))

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
        result = {"claimed": 0, "sent": 0, "removed": 0}
        with self.lock:
            notices, self.notices = self.notices, []
        if not devices.every():
            return result
        key = server_key(Path(config.database).parent)
        devices.keep_key(public_key_id(key))
        targets = devices.every()
        answers = {"refused": set(), "accepted": set()}
        for device, label in notices:
            others = [target for target in targets if target["id"] != device]
            self._round(key, devices, others, notice(device, label), False, result, answers)
        targets, items = self._claim(devices, targets)
        result["claimed"] = len(items)
        for item in items:
            self._round(key, devices, targets, message(item), item["urgent"], result, answers)
        result["removed"] += devices.counted(answers["refused"], answers["accepted"])
        return result

    def _claim(self, devices: Devices, targets: list[dict]) -> tuple[list[dict], list[dict]]:
        """What Pi lets through now, and the devices still on once it answered."""
        config = self.app.state.config
        if not targets or not config.pi_owner_key:
            return targets, []
        try:
            response = self.app.state.client.post(
                config.pi_url.rstrip("/") + "/push/claim",
                headers={"X-Pi-Owner-Key": config.pi_owner_key},
                json={"after": min(target["created"] for target in targets)},
                timeout=10,
            )
        except httpx.HTTPError:
            return targets, []
        # Read again: a device turned off, or sign-out everywhere, during the claim is not woken.
        on = {target["id"] for target in devices.every()}
        return [target for target in targets if target["id"] in on], _claimed(response)

    def _round(self, key, devices: Devices, targets, body: bytes, urgent, result, answers) -> None:
        failed = 0
        for target in targets:
            if target.get("gone") or target["id"] in answers["refused"]:
                continue  # one refusal a round: a refusing push service is not asked again
            status = self._send(key, target, body, urgent)
            if status in GONE:
                devices.remove(target["id"])
                target["gone"] = True
                result["removed"] += 1
            elif status in REFUSED:
                answers["refused"].add(target["id"])
            elif status is not None and 200 <= status < 300:
                answers["accepted"].add(target["id"])
                result["sent"] += 1
            else:
                failed += 1
        if failed:
            LOG.warning("Web push: %d sends were not accepted.", failed)

    def _send(self, key, target: dict, body: bytes, urgent: bool) -> int | None:
        if not endpoint_allowed(target["endpoint"]):
            return None
        try:
            point, auth = device_keys(target["p256dh"], target["auth"])
            response = self.client.post(
                target["endpoint"],
                content=encrypt(body, point, auth),
                headers={
                    "TTL": str(TTL_SECONDS),
                    "Urgency": "high" if urgent else "normal",
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                    "Authorization": authorization(key, target["endpoint"], time.time()),
                },
            )
        except (httpx.HTTPError, ValueError):
            return None
        return response.status_code
