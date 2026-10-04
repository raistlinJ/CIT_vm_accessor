import html
import json
from unittest.mock import patch

import pytest

import main
from test_spice import client, response


POLICY = '{"AccessForge":{"file_upload":true,"file_download":false}}'
LOGIN = '{"Scenario":"Lab","User":"student","Pass":"guest-password"}'


@pytest.mark.parametrize('notes', [LOGIN, LOGIN + '\n\n' + POLICY,
                                 POLICY + '\n\n' + LOGIN,
                                 'Human notes\n' + LOGIN + '\n' + POLICY,
                                 html.escape(LOGIN + '\n' + POLICY),
                                 '<p>' + html.escape(LOGIN) + '</p>\n' + POLICY])
def test_credentials_ignore_separate_transfer_policy(notes):
    assert main._extract_vm_credentials(notes) == {'username': 'student', 'password': 'guest-password'}


@pytest.mark.parametrize('notes', [None, '', POLICY, '{"Scenario":"Lab"}',
                                 '{"User":{"nested":"value"},"Pass":false}', '{broken JSON'])
def test_missing_credentials_never_turn_metadata_into_credentials(notes):
    assert main._extract_vm_credentials(notes) == {'username': '', 'password': ''}


@pytest.mark.parametrize('user_key,pass_key', [('User', 'Pass'), ('VMUser', 'VMPass'),
                                             ('username', 'password'), ('Username', 'Password')])
def test_credential_aliases_and_special_characters_are_preserved(user_key, pass_key):
    user, password = 'student<&>', ' leading } "quotes", &lt; <script>alert(1)</script> trailing '
    notes = json.dumps({user_key: user, pass_key: password}) + '\n' + POLICY
    assert main._extract_vm_credentials(notes) == {'username': user, 'password': password}


def test_plain_text_login_details_remain_supported():
    assert main._extract_vm_credentials('User: "student"\nPassword = guest-password\n' + POLICY) == {
        'username': 'student', 'password': 'guest-password'}


@pytest.mark.parametrize('vtype', ['qemu', 'lxc'])
def test_notes_endpoint_returns_only_credential_fields_in_structured_credentials(client, vtype):
    notes = LOGIN + '\n\n' + POLICY
    with patch.object(main, 'proxmox_get', return_value=response({'description': notes})) as get:
        result = client.get('/api/vm-notes', query_string={'node': 'node', 'vmid': '101', 'type': vtype})
    assert result.status_code == 200
    assert result.json['credentials'] == {'username': 'student', 'password': 'guest-password'}
    assert result.json['notes'] == notes  # Keep the existing API field for compatibility.
    assert get.call_args.args[0] == f'/nodes/node/{vtype}/101/config'


def test_notes_endpoint_still_requires_authentication():
    with main.app.test_client() as client, patch.object(main, 'proxmox_get') as get:
        assert client.get('/api/vm-notes?node=node&vmid=101&type=qemu').status_code == 401
        get.assert_not_called()
