import time
from unittest.mock import patch

import pytest
import requests

import main
from test_spice import client, response


def power_request(client, action="start", origin="http://localhost"):
    client.get("/console/spice/101?node=old-node")
    with client.session_transaction() as session:
        csrf = session["spice_csrf"]
    return client.post("/api/spice/101/power", json={"action": action},
                       headers={"Origin": origin, "X-Console-CSRF": csrf})


def resource(status="stopped", vtype="qemu"):
    return response([{"vmid": 101, "type": vtype, "node": "node-b", "status": status}])


@pytest.mark.parametrize("action,status,operation", [("start", "stopped", "start"),
                                                    ("restart", "running", "reboot")])
def test_power_action_routes_to_current_node_and_tracks_completion(client, action, status, operation):
    with patch.object(main, "proxmox_get", return_value=resource(status)), \
            patch.object(main, "proxmox_post", return_value=response("UPID:node-b:task:")) as post:
        result = power_request(client, action)
    assert result.status_code == 202
    post.assert_called_once_with(f"/nodes/node-b/qemu/101/status/{operation}", data={},
                                 cookies={"PVEAuthCookie": "test-ticket"},
                                 headers={"CSRFPreventionToken": "test-csrf"}, timeout=15)
    task_url = result.json["task_url"]
    for task, done in [({"status": "running"}, False), ({"status": "stopped", "exitstatus": "OK"}, True)]:
        with patch.object(main, "proxmox_get", return_value=response(task)) as get:
            poll = client.get(task_url)
        assert get.call_args.args == ("/nodes/node-b/tasks/UPID%3Anode-b%3Atask%3A/status",)
        assert poll.json["done"] is done
    with patch.object(main, "proxmox_get", return_value=response({"status": "stopped", "exitstatus": "ERROR"})):
        assert client.get(task_url).status_code == 502

    # Signed task handles cannot be tampered with or used for another VM/session.
    with patch.object(main, "proxmox_get") as get:
        assert client.get(task_url.replace("/101/", "/102/")).status_code == 403
        assert client.get(task_url + "tampered").status_code == 403
        with patch("itsdangerous.timed.time.time", return_value=time.time() + 601):
            assert client.get(task_url).status_code == 403
        with client.session_transaction() as session:
            session["pve_ticket"] = "another-session"
        assert client.get(task_url).status_code == 403
        get.assert_not_called()


def test_power_security_rejects_unauthenticated_and_cross_origin_requests(client):
    with patch.object(main, "proxmox_post") as post, patch.object(main, "proxmox_get") as get:
        assert client.get("/api/spice/101/power").status_code == 405
        assert client.post("/api/spice/101/power", json={"action": "start"}).status_code == 403
        assert power_request(client, origin="https://evil.example").status_code == 403
        assert power_request(client, action="reset").status_code == 400
        client.get("/logout")
        assert client.post("/api/spice/101/power", json={"action": "start"}).status_code == 401
        get.assert_not_called()
        post.assert_not_called()


@pytest.mark.parametrize("action,resources,expected", [
    ("start", resource("running"), 200),
    ("restart", resource("stopped"), 409),
    ("start", resource(vtype="lxc"), 404),
    ("start", response([]), 404),
    ("start", response(None, 403), 403),
    ("start", response(None, 500), 502),
])
def test_power_skips_invalid_state_or_unavailable_vm(client, action, resources, expected):
    with patch.object(main, "proxmox_get", return_value=resources), patch.object(main, "proxmox_post") as post:
        result = power_request(client, action)
    assert result.status_code == expected
    post.assert_not_called()


@pytest.mark.parametrize("upstream,expected", [(response(None, 401), 401), (response(None, 403), 403),
                                              (response(None, 500), 502), (requests.Timeout(), 502)])
def test_power_failures_are_reported_without_retrying(client, upstream, expected):
    with patch.object(main, "proxmox_get", return_value=resource()), \
            patch.object(main, "proxmox_post", side_effect=[upstream]) as post:
        result = power_request(client)
    assert result.status_code == expected
    assert "error" in result.json
    assert post.call_count == 1
