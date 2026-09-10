import json
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest
import requests

import main


def response(data, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode()
    return result


def run_reset(vtype='qemu', status='running', snapshots=None, lookup_status=200,
              post_status=200, task_exit='OK'):
    if snapshots is None:
        snapshots = [{'name': 'old', 'snaptime': 1}, {'name': 'baseline', 'snaptime': 2},
                     {'name': 'current', 'snaptime': 999}]

    def get(path, **kwargs):
        if path == '/cluster/resources':
            return response({'data': [{'node': 'pve', 'vmid': 101, 'status': status}]})
        if path.endswith('/snapshot'):
            return response({'data': snapshots, 'message': 'permission denied'} if lookup_status != 200
                            else {'data': snapshots}, lookup_status)
        if '/tasks/' in path:
            return response({'data': {'status': 'stopped', 'exitstatus': task_exit}})
        if path.endswith('/status/current'):
            return response({'data': {'status': status}})
        raise AssertionError(path)

    with main.app.test_client() as client:
        with client.session_transaction() as session:
            session['pve_ticket'] = 'test-ticket'
            session['pve_csrf'] = 'test-csrf'
        with patch.object(main, 'proxmox_get', side_effect=get), patch.object(
            main, 'proxmox_post', return_value=response(
                {'data': 'UPID:test'} if post_status == 200 else
                {'errors': {'start': 'unsupported parameter'}}, post_status)
        ) as post:
            result = client.post('/bulk', data={'action': 'restore-all', 'vms': f'pve|{vtype}|101'})
        return parse_qs(urlparse(result.location).query), post


@pytest.mark.parametrize('vtype', ['qemu', 'lxc'])
@pytest.mark.parametrize('status,start', [('running', 1), ('stopped', 0)])
def test_restores_newest_real_snapshot(vtype, status, start):
    result, post = run_reset(vtype=vtype, status=status)
    assert result['done'] == ['1']
    assert 'baseline' in result['success_list'][0]
    assert post.call_args.args == (f'/nodes/pve/{vtype}/101/snapshot/baseline/rollback',)
    assert post.call_args.kwargs['data'] == {'start': start}


def test_missing_snapshot_is_failure():
    result, post = run_reset(snapshots=[{'name': 'current'}])
    assert result['failed'] == ['1']
    assert result['skipped'] == ['0']
    assert 'no snapshots available' in result['fail_list'][0]
    post.assert_not_called()


def test_permission_error_is_not_reported_as_missing_snapshot():
    result, post = run_reset(lookup_status=403)
    assert result['failed'] == ['1']
    assert 'HTTP 403: permission denied' in result['fail_list'][0]
    post.assert_not_called()


def test_rollback_api_error_is_visible():
    result, _ = run_reset(post_status=400)
    assert result['failed'] == ['1']
    assert 'unsupported parameter' in result['fail_list'][0]


def test_failed_task_is_not_success():
    result, _ = run_reset(task_exit='storage unavailable')
    assert result['done'] == ['0']
    assert result['failed'] == ['1']
    assert 'storage unavailable' in result['fail_list'][0]


def test_expired_session_does_not_reset():
    result, post = run_reset(lookup_status=401)
    assert result['reason'] == ['invalid']
    post.assert_not_called()
