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


@pytest.mark.parametrize('vtype,status,operation', [
    ('qemu', 'running', 'reset'), ('lxc', 'running', 'reboot'),
    ('qemu', 'stopped', 'start'), ('lxc', 'stopped', 'start'),
])
@pytest.mark.parametrize('task_exit', ['OK', 'reset failed'])
def test_backend_reset_excludes_visible_vms_and_tracks_task(vtype, status, operation, task_exit):
    vms = [
        {'node': 'pve', 'vmid': 101, 'type': vtype, 'status': 'running'},
        {'node': 'pve', 'vmid': 102, 'type': vtype, 'status': status},
        {'node': 'pve', 'vmid': 103, 'type': vtype, 'status': 'running'},
    ]

    def get(path, **kwargs):
        if path == '/cluster/resources':
            return response({'data': vms})
        if '/tasks/' in path:
            return response({'data': {'status': 'stopped', 'exitstatus': task_exit}})
        if path.endswith('/status/current'):
            return response({'data': {'status': status}})
        raise AssertionError(path)

    def notes(vm, *args):
        # The backend list mistakenly references visible VMs, including one in another scenario.
        return vm['vmid'], json.dumps({'Scenario': 'lab' if vm['vmid'] != 103 else 'other',
                                       'BackendVMs': [101, 102, 103] if vm['vmid'] == 101 else []})

    with main.app.test_client() as client:
        with client.session_transaction() as session:
            session['pve_ticket'] = 'test-ticket'
            session['pve_csrf'] = 'test-csrf'
        with patch.object(main, 'proxmox_get', side_effect=get), patch.object(
            main, 'fetch_vm_notes', side_effect=notes
        ), patch.object(main, 'proxmox_post', return_value=response({'data': 'UPID:test'})) as post:
            result = client.post('/bulk', data={
                'action': 'factory-reset-scenario', 'scenario': 'lab',
                'vms_visible': '101,103', 'vms': f'pve|{vtype}|101',
            })
        summary = parse_qs(urlparse(result.location).query)
        post.assert_called_once()
        assert post.call_args.args == (f'/nodes/pve/{vtype}/102/status/{operation}',)
        assert summary['done'] == ['1' if task_exit == 'OK' else '0']
        assert summary['failed'] == ['0' if task_exit == 'OK' else '1']
