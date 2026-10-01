"""Exercise console origin checks through the production Waitress stack."""

import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from waitress.server import create_server

import main
from spice_bridge import SpiceBridge
from test_spice import certificates, client, response


@pytest.mark.parametrize("scheme,host,origin,status", [
    # Reproduce the old HTTPS-proxy failure with Waitress's HTTP default.
    (None, "129.108.4.37", "https://129.108.4.37", 403),
    ("https", "129.108.4.37", "https://129.108.4.37", 200),
    ("https", "arlsouth1.utep.edu", "https://arlsouth1.utep.edu", 200),
    ("https", "129.108.4.37:8443", "https://129.108.4.37:8443", 200),
    (None, "localhost:8080", "http://localhost:8080", 200),
    ("https", "129.108.4.37", "http://129.108.4.37", 403),
    ("https", "129.108.4.37", "https://other.example.test", 403),
    ("https", "129.108.4.37:8443", "https://129.108.4.37", 403),
    ("https", "129.108.4.37", "null", 403),
    ("https", "129.108.4.37", None, 403),
])
def test_origin_through_waitress(client, certificates, monkeypatch, scheme, host, origin, status):
    monkeypatch.delenv("PUBLIC_SCHEME", raising=False)
    if scheme is not None:
        monkeypatch.setenv("PUBLIC_SCHEME", scheme)
    monkeypatch.setenv("PORT", "0")
    bridge = SpiceBridge(main.spice_cookie_owner)
    monkeypatch.setattr(main, "spice_bridge", bridge)
    # Obtain exactly the production runner's server options, without starting
    # a separate SPICE listener. The test uses a real Waitress HTTP listener.
    with patch.object(bridge, "start"), patch.object(bridge, "stop"), patch.object(main, "serve") as serve:
        main.run()
    options = dict(serve.call_args.kwargs, host="127.0.0.1", asyncore_loop_timeout=0.05)
    server = create_server(*serve.call_args.args, **options)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        client.get("/console/spice/101")
        with client.session_transaction() as session:
            session["pve_host"] = "129.108.4.37"
            csrf = session["spice_csrf"]
        cookie = client.get_cookie(main.app.config["SESSION_COOKIE_NAME"])
        headers = {
            "Host": host, "Cookie": f"{cookie.key}={cookie.value}",
            "X-Console-CSRF": csrf, "Connection": "close",
            "X-Forwarded-Proto": "https",
            # Untrusted forwarded hosts must not override the public Host.
            "X-Forwarded-Host": "other.example.test",
        }
        if origin is not None:
            headers["Origin"] = origin
        with patch.object(SpiceBridge, "running", True), \
                patch.object(main, "proxmox_get", return_value=response([
                    {"vmid": 101, "type": "qemu", "node": "node-b", "status": "running"}
                ])) as get, \
                patch.object(main, "proxmox_post", return_value=response(certificates.config)) as post:
            with requests.Session() as http:
                http.trust_env = False
                result = http.post(f"http://127.0.0.1:{server.effective_port}/api/spice/101/session",
                                   headers=headers, timeout=5)
        assert result.status_code == status, result.text
        if status == 200:
            token = parse_qs(urlparse(result.json()["websocket"]).query)["token"][0]
            assert bridge.grants[token].origin == origin
            assert bridge.grants[token].target.proxy_host == "129.108.4.37"
            assert post.call_args.kwargs["data"] == {"proxy": "129.108.4.37"}
        else:
            assert result.json()["error"] == "Invalid request origin."
            get.assert_not_called()
            post.assert_not_called()
    finally:
        server.task_dispatcher.shutdown()
        server.close()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_invalid_public_scheme_fails_before_starting_bridge(monkeypatch):
    monkeypatch.setenv("PUBLIC_SCHEME", "ftp")
    with patch.object(main.spice_bridge, "start") as start:
        with pytest.raises(ValueError, match="PUBLIC_SCHEME must be http or https"):
            main.run()
        start.assert_not_called()


def test_forwarded_proto_cannot_override_wsgi_scheme(client):
    client.get("/console/spice/101")
    with client.session_transaction() as session:
        csrf = session["spice_csrf"]
    result = client.post("/api/spice/101/session", headers={
        "X-Console-CSRF": csrf, "Origin": "https://localhost",
        "X-Forwarded-Proto": "https",
    })
    assert result.status_code == 403
    assert result.json["error"] == "Invalid request origin."


@pytest.mark.parametrize("received", [None, "null", "https://other.example.test", "x" * 2000])
def test_origin_rejection_logs_bounded_addresses_without_credentials(client, caplog, received):
    client.get("/console/spice/101")
    with client.session_transaction() as session:
        csrf = session["spice_csrf"]
    headers = {"X-Console-CSRF": csrf}
    if received is not None:
        headers["Origin"] = received
    result = client.post("/api/spice/101/session", headers=headers)
    assert result.status_code == 403
    log = next(r.getMessage() for r in caplog.records if "SPICE origin rejected:" in r.getMessage())
    assert "expected='http://localhost'" in log
    assert f"received={received[:256] if received is not None else None!r}" in log
    assert "host='localhost' scheme='http'" in log
    assert len(log) < 600
    assert csrf not in log
    assert "test-ticket" not in log
    assert "test-csrf" not in log
