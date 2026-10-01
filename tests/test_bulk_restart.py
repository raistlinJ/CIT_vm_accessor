from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

import main
from test_spice import client, response


@pytest.mark.parametrize("vtype", ["qemu", "lxc"])
@pytest.mark.parametrize("status,exitstatus", [("running", "OK"), ("running", "reboot failed"),
                                              ("stopped", "OK")])
def test_restart_selected_reboots_running_guests_and_reports_task_result(client, vtype, status, exitstatus):
    def get(path, **kwargs):
        if path == "/cluster/resources":
            return response([{"node": "pve", "vmid": 101, "status": status},
                             {"node": "pve", "vmid": 102, "status": "running"}])
        if path.endswith("/status/current"):
            return response({"status": status})
        if "/tasks/" in path:
            return response({"status": "stopped", "exitstatus": exitstatus})
        raise AssertionError(path)

    with patch.object(main, "proxmox_get", side_effect=get), \
            patch.object(main, "proxmox_post", return_value=response("UPID:restart")) as post:
        result = client.post("/bulk", data={"action": "restart", "vms": f"pve|{vtype}|101"})
    summary = parse_qs(urlparse(result.location).query)
    if status == "stopped":
        post.assert_not_called()
        assert summary["skipped"] == ["1"]
    else:
        post.assert_called_once_with(f"/nodes/pve/{vtype}/101/status/reboot", data={},
                                     cookies={"PVEAuthCookie": "test-ticket"},
                                     headers={"CSRFPreventionToken": "test-csrf"})
        assert summary["done"] == ["1" if exitstatus == "OK" else "0"]
        assert summary["failed"] == ["0" if exitstatus == "OK" else "1"]
        assert "restart" in summary["success_list" if exitstatus == "OK" else "fail_list"][0]
