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
    assert result.json['ends'] == {}
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
    end = after_logout.json['ends']['student@pve']
    assert end['estimated'] is False
    assert result.json['latest']['student@pve'] <= end['timestamp'] <= after_logout.json['observed_at']


def test_lookup_pairs_latest_session_with_its_last_check_in(login_client):
    store = SignInStore(main.accessforge_data_dir())
    cluster = 'pve.example.test:8006'
    now = main.time.time()
    store.record(cluster, 'student@pve', now - 400, 0, session_id='old')
    store.record(cluster, 'student@pve', now - 300, 0, session_id='new')
    store.touch_presence('new', now - 200)
    store.touch_presence('new', now - 210)
    store.end_presence('old', signed_out_at=now - 20)
    store.record(cluster, 'legacy@pve', now - 300, 0)

    with patch('account_checkout.requests.get', return_value=reply({'/': {'Sys.Modify': 1}})):
        result = login_client.post(LOOKUP, headers={'X-AccessForge-Proxmox-Ticket': 'admin-ticket'},
                                   json={'usernames': ['student@pve', 'legacy@pve']})
    assert result.status_code == 200
    assert result.json['active'] == []
    assert result.json['latest'] == {'student@pve': now - 300, 'legacy@pve': now - 300}
    assert result.json['ends'] == {'student@pve': {'timestamp': now - 200, 'estimated': True}}


def test_rejected_browser_login_never_creates_an_event(login_client):
    ticket = reply({'ticket': 'unusable-ticket', 'CSRFPreventionToken': 'csrf'})
    with patch.object(main, 'proxmox_post', return_value=ticket), \
         patch.object(main, 'proxmox_get', return_value=reply({}, 401)), \
         patch.object(main, 'SignInStore') as store:
        result = login_client.post('/login', data={'username': 'student', 'password': 'password'})
    assert b'Ticket validation failed' in result.data
    store.assert_not_called()


def test_existing_session_can_check_in_without_a_new_sign_in(login_client):
    with login_client.session_transaction() as browser_session:
        browser_session.update(pve_user='tester@pve', pve_ticket='existing-ticket',
                               pve_host='pve.example.test', pve_port='8006')

    page = login_client.get('/console/spice/123')
    assert page.status_code == 200
    assert b'/static/session-presence.js' in page.data

    presence = '/api/session/presence'
    origin = {'Origin': 'http://localhost'}
    with patch.object(main, 'proxmox_get', return_value=reply({}, 401)):
        assert login_client.post(presence, headers=origin).status_code == 401
    with login_client.session_transaction() as browser_session:
        assert 'presence_id' not in browser_session

    with patch.object(main, 'proxmox_get', return_value=reply({'/': {}})) as proxmox:
        assert login_client.post(presence, headers=origin).status_code == 204
        assert proxmox.call_args.kwargs['cookies'] == {'PVEAuthCookie': 'existing-ticket'}
    with patch('account_checkout.requests.get', return_value=reply({'/': {'Sys.Modify': 1}})):
        result = login_client.post(LOOKUP, headers={'X-AccessForge-Proxmox-Ticket': 'admin-ticket'},
                                   json={'usernames': ['tester@pve']})
    assert result.status_code == 200
    assert result.json['active'] == ['tester@pve']
    assert result.json['latest'] == {}


def test_admin_lookup_requires_cluster_admin(login_client):
    assert login_client.post(LOOKUP, json={'usernames': ['student@pve']}).status_code == 401
    admin_ticket = reply({'username': 'root@pam', 'ticket': 'admin-ticket', 'CSRFPreventionToken': 'csrf'})
    with patch('account_checkout.requests.post', return_value=admin_ticket), \
         patch('account_checkout.requests.get', return_value=reply({'/': {}})):
        assert login_client.post(LOOKUP, headers=BASIC, json={'usernames': ['student@pve']}).status_code == 403


@pytest.mark.parametrize('request_headers, upstream_auth', [
    ({'X-AccessForge-Proxmox-Ticket': 'refresh-ticket'}, {'cookies': {'PVEAuthCookie': 'refresh-ticket'}}),
    ({'Authorization': 'PVEAPIToken=student@pve!refresh=example'},
     {'headers': {'Authorization': 'PVEAPIToken=student@pve!refresh=example'}}),
])
def test_sign_in_lookup_authorizes_refresh_credentials(login_client, request_headers, upstream_auth):
    body = {'usernames': ['student@pve']}
    with patch('account_checkout.requests.get', return_value=reply({'/': {'Sys.Modify': 1}})) as upstream:
        result = login_client.post(LOOKUP, headers=request_headers, json=body)
    assert result.status_code == 200
    assert upstream.call_args.args == ('https://pve.example.test:8006/api2/json/access/permissions',)
    for name, value in upstream_auth.items():
        assert upstream.call_args.kwargs[name] == value

    with patch('account_checkout.requests.get', return_value=reply({'/': {'Sys.Audit': 1}})):
        result = login_client.post(LOOKUP, headers=request_headers, json=body)
    assert result.status_code == 403
    assert 'Sys.Modify' in result.json['error']


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
