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
    assert result.json['latest']['student@pve'] > 0
    assert 'guest-password' not in result.text and 'pve-ticket' not in result.text


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
