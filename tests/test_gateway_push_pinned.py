"""The push client connects only to a public address it resolved once, and TLS still checks the
push service's own name (gateway/push_transport.py). No test here reaches the internet."""

import datetime
import hashlib
import ipaddress
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpcore
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient
from test_gateway_push import (
    FCM,
    ORIGIN,
    OWNER_KEY,
    PASSWORD,
    World,
    decrypt,
    device,
    sign_in,
    subscribe,
)

from gateway import push
from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore

HOST = "fcm.googleapis.com"  # the push service FCM's endpoint names
PUBLIC = ("1.2.3.4", "1.2.3.5")  # stand-ins for its addresses; no test connects to them
INSIDE = [
    ("127.0.0.1",),
    ("10.0.0.7",),
    ("172.16.0.1",),
    ("192.168.1.1",),
    ("169.254.169.254",),
    ("100.64.0.1",),  # shared address space (RFC 6598), first address
    ("0.0.0.0",),
    ("224.0.0.1",),
    ("240.0.0.1",),
    ("255.255.255.255",),
    ("::1",),
    ("::",),
    ("fe80::1",),
    ("fc00::1",),
    ("ff02::1",),
    ("::ffff:127.0.0.1",),
    ("::ffff:10.0.0.7",),
    ("64:ff9b::a00:7",),  # NAT64 of 10.0.0.7
    ("2002:a00:7::1",),  # 6to4 of 10.0.0.7
    (PUBLIC[0], "10.0.0.7"),  # one address inside refuses the whole answer
    ("not an address",),
    ("fec0::1",),  # site-local, deprecated but still routed by some networks
]


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """Starts the gateway with one device on, FCM's, and one item to send. Without a transport it
    uses the push client it runs with."""
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    started = []

    def start(push_transport=None):
        auth = AuthStore(tmp_path / "auth.db")
        auth.set_password(PASSWORD)
        state = World()
        config = Config(ORIGIN, str(auth.path), "http://pi", "r" * 32, pi_owner_key=OWNER_KEY)
        app = create_app(
            config,
            store=auth,
            transport=httpx.MockTransport(state.pi_transport),
            push_transport=push_transport,
            push_seconds=None,
        )
        client = TestClient(app, base_url=ORIGIN)
        started.append(client.__enter__())
        keys = device()
        subscribe(client, sign_in(client), FCM, keys[1], keys[2])
        state.claim = {"items": [{"kind": "ask", "urgent": False, "tag": "a" * 16}]}
        return app, state, keys

    yield start
    for client in started:
        client.__exit__(None, None, None)


def answer(*addresses):
    """DNS for HOST answers these addresses, the way getaddrinfo does; other names resolve as
    usual. Also returns the names asked."""
    real, asked = socket.getaddrinfo, []

    def resolve(host, port, *args, **kwargs):
        if host != HOST:
            return real(host, port, *args, **kwargs)
        asked.append(host)
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port, 0, 0))
            if ":" in address
            else (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    return resolve, asked


def no_network(monkeypatch, *addresses) -> tuple[list, list]:
    """The gateway's own push client with DNS answering `addresses` and every connection
    failing; returns the names asked and the connections tried."""
    resolve, asked = answer(*addresses)
    tried = []

    def connect(address, *args, **kwargs):
        tried.append(address)
        raise OSError("no network in tests")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "create_connection", connect)
    return asked, tried


@pytest.mark.parametrize("inside", INSIDE)
def test_a_push_host_that_resolves_inside_the_network_is_never_connected(
    gateway, monkeypatch, inside
):
    app, _, _ = gateway()
    asked, tried = no_network(monkeypatch, *inside)
    assert app.state.push.tick()["sent"] == 0
    assert tried == []  # no connection at all, so not even a TLS handshake reaches it
    assert asked == [HOST]


def test_a_name_that_does_not_resolve_is_never_connected(gateway, monkeypatch):
    app, _, _ = gateway()
    _, tried = no_network(monkeypatch)

    def fails(host, *args, **kwargs):
        raise UnicodeError("label too long") if host == HOST else OSError

    monkeypatch.setattr(socket, "getaddrinfo", fails)
    assert app.state.push.tick()["sent"] == 0 and tried == []


def test_a_public_answer_is_connected_by_address_without_a_second_lookup(gateway, monkeypatch):
    app, _, _ = gateway()
    asked, tried = no_network(monkeypatch, *PUBLIC)
    assert app.state.push.tick()["sent"] == 0
    # Each checked address in turn, as the operating system would, and DNS once.
    assert tried == [(address, 443) for address in PUBLIC] and asked == [HOST]


def test_a_mapped_address_is_judged_by_its_ipv4_address(monkeypatch):
    from gateway import push_transport

    # Python changed how a mapped address is classified within 3.12. Even where the IPv6 tables
    # call it public, the IPv4 address inside decides too.
    monkeypatch.setattr(ipaddress.IPv6Address, "is_global", property(lambda self: True))
    monkeypatch.setattr(ipaddress.IPv6Address, "is_private", property(lambda self: False))
    assert push_transport.public("::ffff:10.0.0.7") is False
    assert push_transport.public("::ffff:" + PUBLIC[0]) is True


def certificates(folder, name: str) -> tuple[str, str, str]:
    """A test authority, and a certificate and key it issued for `name`, as PEM files."""
    now = datetime.datetime.now(datetime.UTC)
    day = datetime.timedelta(days=1)
    authority_key, key = (
        ec.generate_private_key(ec.SECP256R1()),
        ec.generate_private_key(ec.SECP256R1()),
    )
    authority = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test push authority")])
    usage = dict.fromkeys(
        ("content_commitment", "key_encipherment", "data_encipherment", "key_agreement"), False
    )
    root = (
        x509.CertificateBuilder()
        .subject_name(authority)
        .issuer_name(authority)
        .public_key(authority_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - day)
        .not_valid_after(now + day)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
                **usage,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(authority_key.public_key()), critical=False
        )
        .sign(authority_key, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(authority)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - day)
        .not_valid_after(now + day)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(authority_key.public_key()),
            critical=False,
        )
        .sign(authority_key, hashes.SHA256())
    )
    files = (folder / "authority.pem", folder / "service.pem", folder / "service-key.pem")
    files[0].write_bytes(root.public_bytes(serialization.Encoding.PEM))
    files[1].write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    files[2].write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(files[0]), str(files[1]), str(files[2])


class Service:
    """A push service on this machine: TLS with a certificate for `name`, answers 201."""

    def __init__(self, folder, name: str):
        self.authority, chain, key = certificates(folder, name)
        self.names, self.requests = [], []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(chain, key)
        context.sni_callback = lambda _socket, server_name, _context: self.names.append(server_name)
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                service.requests.append((self.path, self.headers["Host"], body))
                self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def pinned(service: Service, *addresses):
    """The pinned transport trusting only the test authority; DNS answers `addresses` and each
    connection to one of them goes to the local service instead."""
    from gateway import push_transport

    resolve, asked = answer(*addresses)
    tried = []

    def connect(address, port, **kwargs):
        tried.append((address, port))
        return httpcore.SyncBackend().connect_tcp("127.0.0.1", service.port, **kwargs)

    context = ssl.create_default_context(cafile=service.authority)
    transport = push_transport.PinnedTransport(verify=context, resolve=resolve, connect=connect)
    return transport, asked, tried


def test_tls_checks_the_push_service_name_on_the_checked_address(gateway, tmp_path):
    service = Service(tmp_path, HOST)
    transport, asked, tried = pinned(service, PUBLIC[0])
    try:
        app, _, keys = gateway(transport)
        assert app.state.push.tick()["sent"] == 1
    finally:
        service.close()
    assert asked == [HOST] and tried == [(PUBLIC[0], 443)]
    assert service.names == [HOST]  # SNI names the push service, not the address
    [(path, host, body)] = service.requests
    assert path == "/fcm/send/device-one" and host == HOST
    assert decrypt(body, *keys)["title"] == push.TITLES["ask"]


def test_a_certificate_for_another_name_is_refused_and_nothing_is_posted(gateway, tmp_path):
    service = Service(tmp_path, "elsewhere.test")
    transport, _, tried = pinned(service, PUBLIC[0])
    try:
        app, _, _ = gateway(transport)
        assert app.state.push.tick()["sent"] == 0
    finally:
        service.close()
    assert tried == [(PUBLIC[0], 443)] and service.requests == []
