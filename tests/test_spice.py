import datetime
import errno
import gc
import json
import socket
import socketserver
import ssl
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest
import requests
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

import main
import spice_bridge as bridge_module
from spice_bridge import SpiceBridge, Target, check_subject, session_owner


def response(data, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps({"data": data}).encode()
    return result


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("spice-tls")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test cluster CA")])
    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "PVE Cluster Node"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Proxmox Virtual Environment"),
        x509.NameAttribute(NameOID.COMMON_NAME, "node-b.example.test"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)

    def builder(subject, public_key):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(ca_name)
                .public_key(public_key).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(days=1)))

    ca = (builder(ca_name, ca_key.public_key())
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    cert = builder(name, key.public_key()).sign(ca_key, hashes.SHA256())
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    config = {"type": "spice", "host": "pvespiceproxy:deadbeef:101:node-b::abcd",
              "tls-port": 61000, "password": "short-lived-spice-password",
              "host-subject": "OU=PVE Cluster Node,O=Proxmox Virtual Environment,CN=node-b.example.test",
              "ca": ca.public_bytes(serialization.Encoding.PEM).decode().replace("\n", "\\n"),
              "title": "VM 101 - Test"}
    return SimpleNamespace(config=config, context=context, cert=cert)


@pytest.fixture
def client():
    with main.app.test_client() as client:
        with client.session_transaction() as session:
            session.update(pve_ticket="test-ticket", pve_csrf="test-csrf",
                           pve_login_time=time.time(), pve_host="node-a.example.test")
        yield client


def session_request(client, **kwargs):
    client.get("/console/spice/101")
    with client.session_transaction() as session:
        csrf = session["spice_csrf"]
    return client.post("/api/spice/101/session", headers={
        "X-Console-CSRF": csrf, "Origin": "http://localhost",
    }, **kwargs)


@pytest.mark.parametrize("method", ["get", "post"])
def test_vm_defaults_to_spice(client, method):
    kwargs = {"query_string" if method == "get" else "data": {
        "node": "node-a", "vmid": "101", "vtype": "qemu"}}
    assert getattr(client, method)("/open", **kwargs).location == "/console/spice/101?node=node-a"


@pytest.mark.parametrize("vtype,mode", [("lxc", "xtermjs"), ("qemu", "novnc")])
def test_existing_consoles_preserved(client, vtype, mode):
    result = client.get("/open", query_string={"node": "node-b", "vmid": "101", "vtype": vtype,
                                             "console": "novnc"})
    url = urlparse(result.location)
    assert url.path == "/proxmox/"
    assert parse_qs(url.query)[mode] == ["1"]


def test_session_requires_login_and_csrf(client):
    assert client.post("/api/spice/101/session").status_code == 403
    client.get("/logout")
    assert client.post("/api/spice/101/session").status_code == 401
    assert client.get("/console/spice/101").status_code == 302


def test_fallback_available_even_when_bridge_is_down(client):
    result = client.get("/console/spice/101?node=node-a")
    assert b"console=novnc" in result.data
    with patch.object(SpiceBridge, "running", False):
        assert session_request(client).status_code == 503


def test_websocket_cookie_rejects_tampering_and_expired_login(client):
    assert main.spice_cookie_owner("session=forged") is None
    with client.session_transaction() as session:
        session["pve_login_time"] = time.time() - 111 * 60
    assert spice_owner_from_client(client) is None


def test_session_rejects_cross_origin(client):
    client.get("/console/spice/101")
    with client.session_transaction() as session:
        csrf = session["spice_csrf"]
    result = client.post("/api/spice/101/session", headers={
        "X-Console-CSRF": csrf, "Origin": "https://other.example.test"})
    assert result.status_code == 403


def test_resolves_migrated_vm_and_keeps_routing_credentials_server_side(client, certificates, monkeypatch):
    bridge = SpiceBridge(main.spice_cookie_owner)
    monkeypatch.setattr(main, "spice_bridge", bridge)
    with patch.object(SpiceBridge, "running", True), patch.object(main, "proxmox_get", return_value=response([
        {"vmid": 101, "type": "qemu", "node": "node-b", "status": "running"}
    ])), patch.object(main, "proxmox_post", return_value=response(certificates.config)) as post:
        result = session_request(client)
    assert result.status_code == 200
    assert result.headers["Cache-Control"] == "no-store"
    assert post.call_args.args == ("/nodes/node-b/qemu/101/spiceproxy",)
    assert post.call_args.kwargs["data"] == {"proxy": "node-a.example.test"}
    assert result.json["node"] == "node-b"
    assert result.json["password"] == certificates.config["password"]
    assert "pvespiceproxy" not in result.text
    assert "BEGIN CERTIFICATE" not in result.text
    token = parse_qs(urlparse(result.json["websocket"]).query)["token"][0]
    assert bridge.grants[token].target.proxy_host == "node-a.example.test"
    assert bridge.grants[token].target.context.verify_mode == ssl.CERT_REQUIRED
    assert spice_owner_from_client(client) == session_owner("test-ticket")


def spice_owner_from_client(client):
    cookie = client.get_cookie(main.app.config["SESSION_COOKIE_NAME"])
    return main.spice_cookie_owner(f"{cookie.key}={cookie.value}")


@pytest.mark.parametrize("resources,expected", [
    ([], 404),
    ([{"vmid": 101, "type": "lxc", "node": "node-b", "status": "running"}], 404),
    ([{"vmid": 101, "type": "qemu", "node": "node-b", "status": "stopped"}], 409),
])
def test_does_not_issue_ticket_for_unavailable_vm(client, resources, expected):
    with patch.object(SpiceBridge, "running", True), patch.object(main, "proxmox_get", return_value=response(resources)), \
            patch.object(main, "proxmox_post") as post:
        result = session_request(client)
    assert result.status_code == expected
    post.assert_not_called()


@pytest.mark.parametrize("upstream,expected", [(401, 401), (403, 403), (500, 502)])
def test_spice_api_failure_keeps_fallback(client, upstream, expected):
    with patch.object(SpiceBridge, "running", True), patch.object(main, "proxmox_get", return_value=response([
        {"vmid": 101, "type": "qemu", "node": "node-b", "status": "running"}
    ])), patch.object(main, "proxmox_post", return_value=response(None, upstream)):
        result = session_request(client)
    assert result.status_code == expected
    assert "console=novnc" in result.json["fallback"]


def test_spice_api_response_is_not_logged(client, certificates, caplog):
    with main.app.test_request_context("/"), patch.object(main.requests, "request", return_value=response(certificates.config)):
        with caplog.at_level("INFO"):
            main.proxmox_post("/nodes/node-b/qemu/101/spiceproxy")
    assert certificates.config["password"] not in caplog.text
    assert certificates.config["host"] not in caplog.text


def test_invalid_tls_config_reports_error_without_exposing_credentials(client, certificates):
    config = {**certificates.config, "ca": "not a certificate"}
    with patch.object(SpiceBridge, "running", True), patch.object(main, "proxmox_get", return_value=response([
        {"vmid": 101, "type": "qemu", "node": "node-b", "status": "running"}
    ])), patch.object(main, "proxmox_post", return_value=response(config)):
        result = session_request(client)
    assert result.status_code == 502
    assert config["password"] not in result.text
    assert config["host"] not in result.text
    assert "console=novnc" in result.json["fallback"]


def test_bad_routing_ticket_and_subject_are_rejected(certificates):
    config = {**certificates.config, "host": "pvespiceproxy:abc\r\nInjected: yes"}
    with pytest.raises(ValueError):
        Target.from_config(config, "node-a")
    with pytest.raises(ssl.SSLCertVerificationError):
        check_subject(certificates.cert.public_bytes(serialization.Encoding.DER),
                      x509.Name.from_rfc4514_string("CN=wrong-node"))


@pytest.fixture
def proxy(certificates):
    authorities = []
    closed = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            header = b""
            while not header.endswith(b"\r\n\r\n"):
                data = self.request.recv(1)
                if not data:
                    return
                header += data
            authorities.append(header)
            # Proxmox validates the routing ticket in Host, not the CONNECT
            # request line. Reject missing/wrong Host before attempting TLS.
            expected_host = (certificates.config["host"] + ":" + str(certificates.config["tls-port"])).encode()
            headers = dict(line.split(b":", 1) for line in header.split(b"\r\n")[1:] if line)
            if headers.get(b"Host", b"").strip() != expected_host:
                self.request.sendall(b"HTTP/1.0 401 invalid ticket\r\n\r\n")
                closed.set()
                return
            self.request.sendall(b"HTTP/1.0 200 Connection established\r\n\r\n")
            try:
                with certificates.context.wrap_socket(self.request, server_side=True) as stream:
                    stream.sendall(b"server hello")
                    while data := stream.recv(65536):
                        stream.sendall(data)
            except (OSError, ssl.SSLError):
                pass
            finally:
                closed.set()

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    with Server(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield SimpleNamespace(port=server.server_address[1], authorities=authorities, closed=closed)
        server.shutdown()
        thread.join(timeout=2)


@pytest.fixture
def live_bridge(certificates, proxy):
    bridge = SpiceBridge(lambda cookie: "owner" if cookie == "session=valid" else None)
    bridge.start(port=0)
    port = bridge.server.sockets[0].getsockname()[1]
    target = Target.from_config(certificates.config, "127.0.0.1", proxy.port)
    token = bridge.issue(target, "owner", "https://accessforge.test", 60)
    yield SimpleNamespace(bridge=bridge, url=f"ws://127.0.0.1:{port}/spice/ws?token={token}", token=token)
    bridge.stop()


def websocket(url, **kwargs):
    return connect(url, origin=kwargs.pop("origin", "https://accessforge.test"),
                   additional_headers={"Cookie": kwargs.pop("cookie", "session=valid")},
                   subprotocols=["binary"], proxy=None, **kwargs)


def read_bytes(ws, length):
    result = b""
    while len(result) < length:
        result += ws.recv(timeout=5)
    return result


def test_real_connect_tls_and_multichannel_binary_relay(live_bridge, proxy):
    with websocket(live_bridge.url) as main_channel, websocket(live_bridge.url) as display_channel:
        for ws in (main_channel, display_channel):
            assert ws.subprotocol == "binary"
            assert ws.recv(timeout=5) == b"server hello"
            payload = bytes(range(256)) * 1024
            ws.send(payload)
            assert read_bytes(ws, len(payload)) == payload
        assert len(proxy.authorities) == 2
        assert all(h == (b"CONNECT pvespiceproxy:deadbeef:101:node-b::abcd:61000 HTTP/1.0\r\n"
                         b"Host: pvespiceproxy:deadbeef:101:node-b::abcd:61000\r\n\r\n")
                   for h in proxy.authorities)
    assert proxy.closed.wait(5)


def test_proxmox_proxy_requires_host_even_with_valid_connect_target(proxy):
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as stream:
        stream.sendall(b"CONNECT pvespiceproxy:deadbeef:101:node-b::abcd:61000 HTTP/1.0\r\n\r\n")
        assert stream.recv(1024).startswith(b"HTTP/1.0 401")


def test_proxy_rejection_logs_status_without_routing_ticket(live_bridge, caplog):
    target = live_bridge.bridge.grants[live_bridge.token].target
    target.authority = "pvespiceproxy:expired-secret-ticket:61000"
    with websocket(live_bridge.url) as ws:
        with pytest.raises(ConnectionClosed) as exc:
            ws.recv(timeout=5)
        assert exc.value.rcvd.code == 1011
    assert "Proxmox SPICE CONNECT rejected (HTTP 401)" in caplog.text
    assert target.authority not in caplog.text


@pytest.mark.parametrize("kwargs", [{"origin": "https://attacker.test"}, {"cookie": "session=invalid"}])
def test_websocket_rejects_other_origin_or_session(live_bridge, proxy, kwargs):
    with pytest.raises(InvalidStatus) as exc:
        with websocket(live_bridge.url, **kwargs):
            pass
    assert exc.value.response.status_code == 403
    assert not proxy.authorities


def test_expired_grant_rejected_before_network_access(live_bridge, proxy):
    live_bridge.bridge.grants[live_bridge.token].connect_until = time.monotonic() - 1
    with pytest.raises(InvalidStatus):
        with websocket(live_bridge.url):
            pass
    assert not proxy.authorities


def test_logout_closes_active_tunnels(live_bridge, proxy):
    with websocket(live_bridge.url) as ws:
        assert ws.recv(timeout=5) == b"server hello"
        live_bridge.bridge.revoke("owner")
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=5)
    assert proxy.closed.wait(5)
    assert not live_bridge.bridge.grants


def test_tls_identity_mismatch_fails_closed(live_bridge):
    live_bridge.bridge.grants[live_bridge.token].target.subject = x509.Name.from_rfc4514_string("CN=wrong-node")
    with websocket(live_bridge.url) as ws:
        with pytest.raises(ConnectionClosed) as exc:
            ws.recv(timeout=5)
        assert exc.value.rcvd.code == 1011


def test_untrusted_tls_ca_fails_closed(live_bridge):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    # No trusted roots: even a matching subject must not bypass chain checks.
    live_bridge.bridge.grants[live_bridge.token].target.context = context
    with websocket(live_bridge.url) as ws:
        with pytest.raises(ConnectionClosed) as exc:
            ws.recv(timeout=5)
        assert exc.value.rcvd.code == 1011


def test_text_websocket_frames_are_rejected(live_bridge):
    with websocket(live_bridge.url) as ws:
        assert ws.recv(timeout=5) == b"server hello"
        ws.send("not binary")
        with pytest.raises(ConnectionClosed) as exc:
            ws.recv(timeout=5)
        assert exc.value.rcvd.code == 1003


def test_session_and_channel_limits(certificates):
    bridge = SpiceBridge(lambda _: "owner", max_sessions=1, max_channels=1)
    target = Target.from_config(certificates.config, "node-a")
    token = bridge.issue(target, "owner", "https://accessforge.test", 60)
    with pytest.raises(RuntimeError):
        bridge.issue(target, "owner", "https://accessforge.test", 60)
    bridge.grants[token].connections.add("existing-channel")
    request = SimpleNamespace(path=f"/spice/ws?token={token}", headers={"Origin": "https://accessforge.test"})
    assert bridge._lookup(request) is None


@pytest.mark.parametrize("denied_errno", [errno.EACCES, errno.EPERM])
def test_bridge_relays_and_shuts_down_when_native_socketpair_is_denied(certificates, proxy, denied_errno):
    bridge = SpiceBridge(lambda cookie: "owner" if cookie == "session=valid" else None)
    try:
        with patch.object(bridge_module.socket, "socketpair", side_effect=PermissionError(denied_errno, "denied")):
            bridge.start(port=0)
        assert bridge.loop._ssock.family == socket.AF_INET
        assert bridge.loop._ssock.getsockname()[0] == "127.0.0.1"
        port = bridge.server.sockets[0].getsockname()[1]
        target = Target.from_config(certificates.config, "127.0.0.1", proxy.port)
        token = bridge.issue(target, "owner", "https://accessforge.test", 60)
        with websocket(f"ws://127.0.0.1:{port}/spice/ws?token={token}") as ws:
            assert ws.recv(timeout=5) == b"server hello"
            payload = bytes(range(256)) * 1024
            ws.send(payload)
            assert read_bytes(ws, len(payload)) == payload
            # Revocation schedules a coroutine from the Flask thread. It must
            # wake the loop through the TCP pair, then close the active tunnel.
            bridge.revoke("owner")
            with pytest.raises(ConnectionClosed):
                ws.recv(timeout=5)
    finally:
        bridge.stop()
    assert not bridge.running
    assert bridge.loop.is_closed()
    assert proxy.closed.wait(5)


def test_native_socketpair_remains_the_default():
    bridge = SpiceBridge(lambda _: None)
    try:
        with patch.object(bridge_module, "_loopback_socketpair") as fallback:
            bridge.start(port=0)
        fallback.assert_not_called()
    finally:
        bridge.stop()


def test_event_loop_failure_is_reported_without_coroutine_or_destructor_warnings(recwarn):
    bridge = SpiceBridge(lambda _: None)
    denied = PermissionError(errno.EACCES, "denied")
    with patch.object(bridge_module.socket, "socketpair", side_effect=denied), \
            patch.object(bridge_module, "_loopback_socketpair", side_effect=denied):
        with pytest.raises(RuntimeError, match="initialize its event loop") as exc:
            bridge.start(port=0)
    assert isinstance(exc.value.__cause__, PermissionError)
    bridge.thread.join(timeout=2)
    assert not bridge.running
    gc.collect()
    assert not list(recwarn)


def test_resource_exhaustion_does_not_trigger_socketpair_fallback():
    bridge = SpiceBridge(lambda _: None)
    with patch.object(bridge_module.socket, "socketpair", side_effect=OSError(errno.EMFILE, "too many files")), \
            patch.object(bridge_module, "_loopback_socketpair") as fallback:
        with pytest.raises(RuntimeError, match="initialize its event loop"):
            bridge.start(port=0)
    fallback.assert_not_called()


def test_listener_bind_failure_is_reported_separately():
    bridge = SpiceBridge(lambda _: None)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        with pytest.raises(RuntimeError, match=f"bind its WebSocket listener on 127.0.0.1:{port}"):
            bridge.start(port=port)
    bridge.thread.join(timeout=2)
    assert not bridge.running
    assert bridge.loop.is_closed()
