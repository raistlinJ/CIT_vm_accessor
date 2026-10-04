import base64
import logging
from unittest.mock import patch

import pytest

import main
from test_spice import client, response


def read_request(client, path="/home/user/report.bin", offset=0, origin="http://localhost"):
    client.get("/console/spice/101?node=node-a")
    with client.session_transaction() as session:
        csrf = session["spice_csrf"]
    return client.post("/api/spice/101/file-read", json={"path": path, "offset": offset},
                       headers={"X-Console-CSRF": csrf, "Origin": origin})


def vm_resources(status="running"):
    return response([{"vmid": 101, "node": "node-b", "type": "qemu", "status": status}])


def test_disabled_switches_hide_controls_and_block_endpoint(client, monkeypatch):
    monkeypatch.delenv("ENABLE_VM_FILE_TRANSFER", raising=False)
    monkeypatch.delenv("ENABLE_VM_FILE_UPLOAD", raising=False)
    monkeypatch.delenv("ENABLE_VM_FILE_DOWNLOAD", raising=False)
    assert b"file-panel" not in client.get("/console/spice/101").data
    with patch.object(main, "proxmox_get") as get:
        assert read_request(client).status_code == 404
        get.assert_not_called()


@pytest.mark.parametrize("upload,download", [("true", "false"), ("false", "TRUE"),
                                              (" true ", " true ")])
def test_directional_switches_show_only_enabled_controls(client, monkeypatch, upload, download):
    monkeypatch.setenv("ENABLE_VM_FILE_UPLOAD", upload)
    monkeypatch.setenv("ENABLE_VM_FILE_DOWNLOAD", download)
    page = client.get("/console/spice/101").data
    assert (b"file-upload" in page) == (upload.strip().lower() == "true")
    assert (b"file-download" in page) == (download.strip().lower() == "true")
    assert b"file-panel" in page
    with patch.object(main, "proxmox_get") as get:
        if download.strip().lower() != "true":
            assert read_request(client).status_code == 404
            get.assert_not_called()


def test_legacy_switch_falls_back_per_direction(client, monkeypatch):
    monkeypatch.setenv("ENABLE_VM_FILE_TRANSFER", "true")
    monkeypatch.delenv("ENABLE_VM_FILE_UPLOAD", raising=False)
    monkeypatch.delenv("ENABLE_VM_FILE_DOWNLOAD", raising=False)
    page = client.get("/console/spice/101").data
    assert b"file-upload" in page
    assert b"file-download" in page
    monkeypatch.setenv("ENABLE_VM_FILE_DOWNLOAD", "false")
    page = client.get("/console/spice/101").data
    assert b"file-upload" in page
    assert b"file-download" not in page
    with patch.object(main, "proxmox_get") as get:
        assert read_request(client).status_code == 404
        get.assert_not_called()


def test_reads_binary_chunk_from_current_cluster_node(client, monkeypatch):
    monkeypatch.setenv("ENABLE_VM_FILE_DOWNLOAD", "true")
    chunk = b"\x00\xffcaf\xc3\xa9\n"
    with patch.object(main, "proxmox_get", side_effect=[vm_resources(), response({"description": '{"AccessForge":{"file_upload":true,"file_download":true}}'}), response({
        "content": base64.b64encode(chunk).decode(), "bytes-read": len(chunk), "truncated": 1,
    })]) as get:
        result = read_request(client, offset=17)
    assert result.status_code == 200
    assert result.data == chunk
    assert result.headers["Content-Type"] == "application/octet-stream"
    assert result.headers["Cache-Control"] == "no-store"
    assert result.headers["X-File-More"] == "true"
    assert get.call_args.args == ("/nodes/node-b/qemu/101/agent/file-read",)
    assert get.call_args.kwargs["params"] == {"file": "/home/user/report.bin", "offset": 17,
                                             "count": 1024 * 1024, "decode": 0}
    assert get.call_args.kwargs["cookies"] == {"PVEAuthCookie": "test-ticket"}


@pytest.mark.parametrize("path,offset", [
    ("relative/file", 0), ("/bad\x00name", 0), ("/bad\nname", 0),
    ("/file", -1), ("/file", 64 * 1024 * 1024 + 1), ("/file", True),
])
def test_rejects_invalid_file_requests(client, monkeypatch, path, offset):
    monkeypatch.setenv("ENABLE_VM_FILE_DOWNLOAD", "true")
    with patch.object(main, "proxmox_get") as get:
        assert read_request(client, path, offset).status_code == 400
        get.assert_not_called()


def test_file_read_requires_session_csrf_and_origin(client, monkeypatch):
    monkeypatch.setenv("ENABLE_VM_FILE_DOWNLOAD", "true")
    with patch.object(main, "proxmox_get") as get:
        assert client.post("/api/spice/101/file-read", json={"path": "/x", "offset": 0}).status_code == 403
        assert read_request(client, origin="https://evil.example").status_code == 403
        client.get("/logout")
        assert client.post("/api/spice/101/file-read", json={"path": "/x", "offset": 0}).status_code == 401
        get.assert_not_called()


@pytest.mark.parametrize("upstream,expected", [
    (vm_resources("stopped"), 409), (response([]), 404), (response(None, 403), 403),
])
def test_file_read_requires_running_authorized_vm(client, monkeypatch, upstream, expected):
    monkeypatch.setenv("ENABLE_VM_FILE_DOWNLOAD", "true")
    with patch.object(main, "proxmox_get", side_effect=[upstream]) as get:
        assert read_request(client).status_code == expected
        assert get.call_count == 1


def test_file_read_enforces_upstream_permission_and_validates_data(client, monkeypatch):
    monkeypatch.setenv("ENABLE_VM_FILE_DOWNLOAD", "true")
    for upstream, expected in [
        (response(None, 403), 403),
        (response({"content": "not base64", "bytes-read": 10}), 502),
        (response({"content": "", "bytes-read": 0, "truncated": 1}), 502),
    ]:
        with patch.object(main, "proxmox_get", side_effect=[vm_resources(), response({"description": '{"AccessForge":{"file_download":true}}'}), upstream]):
            assert read_request(client).status_code == expected


def test_proxmox_file_content_and_path_are_not_logged(client, caplog):
    secret = "private-file-contents"
    path = "/guest/private/secret-file"
    with main.app.test_request_context("/"), patch.object(main.requests, "request", return_value=response({
        "content": base64.b64encode(secret.encode()).decode(), "bytes-read": len(secret),
    })):
        with caplog.at_level(logging.INFO):
            main.proxmox_get("/nodes/node-b/qemu/101/agent/file-read", params={"file": path})
    assert secret not in caplog.text
    assert path not in caplog.text
    assert base64.b64encode(secret.encode()).decode() not in caplog.text
