from contextlib import closing
import sqlite3
from unittest.mock import patch

import pytest

import main
from sign_in_store import SignInStore
from test_account_checkout import BASIC, reply


LOOKUP = '/api/admin/sign-ins/latest'


def test_pruning_preserves_latest_per_account_and_cluster(tmp_path):
    store = SignInStore(tmp_path)
    store.record('cluster-a', 'student@pve', 20, 2)
    store.record('cluster-a', 'student@pam', 30, 2)
    store.record('cluster-b', 'student@pve', 40, 2)
    store.record('cluster-a', 'student@pam', 25, 2)

    reopened = SignInStore(tmp_path)
    assert reopened.latest('cluster-a', ['student@pve', 'student@pam']) == {'student@pve': 20, 'student@pam': 30}
    assert reopened.latest('cluster-b', ['student@pve']) == {'student@pve': 40}
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute('SELECT timestamp FROM sign_in_events ORDER BY id').fetchall() == [(40,), (25,)]
    reopened.record('cluster-a', 'student@pve', 50, 0)
    assert reopened.latest('cluster-a', ['student@pve']) == {'student@pve': 50}
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute('SELECT COUNT(*) FROM sign_in_events').fetchone() == (0,)


def test_presence_expires_and_tracks_sessions_independently(tmp_path):
    store = SignInStore(tmp_path)
    store.record('cluster-a', 'student@pve', 100, 0, session_id='first')
    store.record('cluster-a', 'student@pve', 120, 0, session_id='second')
    store.record('cluster-b', 'student@pam', 120, 0, session_id='other')
    requested = ['student@pve', 'student@pam']
    assert store.active_accounts('cluster-a', requested, 150, 100) == ['student@pve']
    store.end_presence('first')
    assert not store.touch_presence('first', 150)
    assert store.active_accounts('cluster-a', requested, 219, 100) == ['student@pve']
    assert store.active_accounts('cluster-a', requested, 220, 100) == []
    assert store.touch_presence('second', 220)
    assert store.active_accounts('cluster-a', requested, 221, 100) == ['student@pve']
    store.end_presence('second')
    assert store.active_accounts('cluster-a', requested, 222, 100) == []
    assert store.latest('cluster-a', requested) == {'student@pve': 120}


@pytest.fixture
def login_client(tmp_path, monkeypatch):
    monkeypatch.setenv('ENABLE_ACCOUNT_CHECKOUT', 'false')
    monkeypatch.setitem(main.app.config, 'CHECKOUT_DATA_DIR', str(tmp_path))
    monkeypatch.setitem(main.app.config, 'CHECKOUT_PROXMOX_HOST', 'pve.example.test')
    monkeypatch.setitem(main.app.config, 'CHECKOUT_PROXMOX_PORT', '8006')
    return main.app.test_client()


def test_validated_browser_login_is_returned_by_admin_lookup(login_client):
    ticket = reply({'username': 'student@pve', 'ticket': 'pve-ticket', 'CSRFPreventionToken': 'csrf'})
    form = {'username': 'student', 'password': 'guest-password', 'realm': 'pve',
            'host': 'PVE.EXAMPLE.TEST.', 'port': '8006'}
    with patch.object(main, 'proxmox_post', return_value=ticket), patch.object(main, 'proxmox_get', return_value=reply({})):
        assert login_client.post('/login', data=form).status_code == 302

    query = {'usernames': ['student@pve', 'student@pam', 'root@pam']}
    admin_ticket = reply({'username': 'root@pam', 'ticket': 'admin-ticket', 'CSRFPreventionToken': 'csrf'})
    with patch('account_checkout.requests.post', return_value=admin_ticket), \
         patch('account_checkout.requests.get', return_value=reply({'/': {'Sys.Modify': 1}})):
        result = login_client.post(LOOKUP, headers=BASIC, json=query)
    assert result.status_code == 200
    assert result.headers['Cache-Control'] == 'no-store'
    assert result.json['cluster'] == {'host': 'pve.example.test', 'port': '8006'}
    assert set(result.json['latest']) == {'student@pve'}
    assert 0 < result.json['latest']['student@pve'] <= result.json['observed_at']
    assert result.json['active'] == ['student@pve']
    assert 'guest-password' not in result.text and 'pve-ticket' not in result.text

    presence = '/api/session/presence'
    assert login_client.post(presence, headers={'Origin': 'https://other.example'}).status_code == 403
    with patch.object(main, 'proxmox_get', return_value=reply({'/': {}})) as proxmox:
        assert login_client.post(presence, headers={'Origin': 'http://localhost'}).status_code == 204
        assert proxmox.call_args.kwargs['cookies'] == {'PVEAuthCookie': 'pve-ticket'}
    assert login_client.get('/logout').status_code == 302
    assert login_client.post(presence, headers={'Origin': 'http://localhost'}).status_code == 401
    with patch('account_checkout.requests.post', return_value=admin_ticket), \
         patch('account_checkout.requests.get', return_value=reply({'/': {'Sys.Modify': 1}})):
        after_logout = login_client.post(LOOKUP, headers=BASIC, json=query)
    assert after_logout.json['active'] == []
    assert after_logout.json['latest'] == result.json['latest']


def test_rejected_browser_login_never_creates_an_event(login_client):
    ticket = reply({'ticket': 'unusable-ticket', 'CSRFPreventionToken': 'csrf'})
    with patch.object(main, 'proxmox_post', return_value=ticket), \
         patch.object(main, 'proxmox_get', return_value=reply({}, 401)), \
         patch.object(main, 'SignInStore') as store:
        result = login_client.post('/login', data={'username': 'student', 'password': 'password'})
    assert b'Ticket validation failed' in result.data
    store.assert_not_called()


def test_admin_lookup_requires_cluster_admin(login_client):
    assert login_client.post(LOOKUP, json={'usernames': ['student@pve']}).status_code == 401
    admin_ticket = reply({'username': 'root@pam', 'ticket': 'admin-ticket', 'CSRFPreventionToken': 'csrf'})
    with patch('account_checkout.requests.post', return_value=admin_ticket), \
         patch('account_checkout.requests.get', return_value=reply({'/': {}})):
        assert login_client.post(LOOKUP, headers=BASIC, json={'usernames': ['student@pve']}).status_code == 403


@pytest.mark.parametrize('matching', [True, False])
def test_admin_lookup_verifies_alternate_cluster_node(login_client, monkeypatch, matching):
    import ipaddress

    monkeypatch.setitem(main.app.config, 'CHECKOUT_PROXMOX_HOST', '192.0.2.45')
    store = SignInStore(main.accessforge_data_dir())
    store.record('192.0.2.45:8006', 'student@pve', 30, 10)
    store.record('node-3.example.test:8006', 'student@pve', 40, 10)
    store.record('192.0.2.44:8006', 'student@pve', 50, 10)
    monkeypatch.setattr(main, 'resolve_proxmox_addresses', lambda host: {ipaddress.ip_address(
        '192.0.2.45' if host == '192.0.2.45' else '192.0.2.44' if matching else '198.51.100.4')})
    admin_ticket = reply({'username': 'root@pam', 'ticket': 'admin-ticket', 'CSRFPreventionToken': 'csrf'})
    topology = reply([{'type': 'cluster', 'name': 'test'}, {'type': 'node', 'ip': '192.0.2.44'}])
    with patch('account_checkout.requests.post', return_value=admin_ticket), \
         patch('account_checkout.requests.get', side_effect=[reply({'/': {'Sys.Modify': 1}}), topology]) as get:
        result = login_client.post(LOOKUP, headers=BASIC, json={
            'usernames': ['student@pve'], 'cluster': {'host': 'node-3.example.test', 'port': 8006},
        })
    assert get.call_args.args == ('https://192.0.2.45:8006/api2/json/cluster/status',)
    assert get.call_args.kwargs['cookies'] == {'PVEAuthCookie': 'admin-ticket'}
    if matching:
        assert result.status_code == 200
        assert result.json['cluster'] == {'host': 'node-3.example.test', 'port': 8006}
        assert result.json['latest'] == {'student@pve': 50}
    else:
        assert result.status_code == 409
        assert 'latest' not in result.json
