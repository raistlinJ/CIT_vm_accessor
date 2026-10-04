import json
import struct
from unittest.mock import patch

import pytest
import main
from test_spice import client, response, websocket, live_bridge, proxy, certificates, read_bytes
from test_vm_files import read_request, vm_resources
from transfer_policy import parse_transfer_policy
from spice_policy import SpiceClientInspector, SpicePolicyError


def link(channel=1):
    return (struct.pack('<4sIII', b'REDQ', 2, 2, 22) +
            struct.pack('<IBBIII', 0, channel, 0, 1, 0, 18) + struct.pack('<I', 9))


def handshake(channel=1):
    return link(channel) + struct.pack('<I', 1) + bytes(128)


def packet(kind, body):
    return struct.pack('<HI', kind, len(body)) + body


def agent(kind, body=b'file'):
    return struct.pack('<IIQI', 1, kind, 0, len(body)) + body


@pytest.mark.parametrize('notes', [None, '', '{}', '{"AccessForge":true}',
    '{"AccessForge":{"file_upload":"true"}}',
    '{"AccessForge":{"file_upload":false,"file_upload":true}}',
    '{"AccessForge":{"file_upload":true}}\n{"AccessForge":{"file_upload":true}}',
    '{broken "AccessForge": {"file_upload":true}}'])
def test_policy_denies_missing_malformed_or_duplicate_metadata(notes):
    assert parse_transfer_policy(notes) == {'file_upload': False, 'file_download': False}


def test_policy_preserves_direction_and_ignores_other_notes():
    notes = 'Human notes\n{"Scenario":"demo"}\n{"AccessForge":{"file_upload":true,"file_download":false}}'
    assert parse_transfer_policy(notes) == {'file_upload': True, 'file_download': False}


def test_inspector_detects_uploads_across_every_byte_boundary():
    wire = handshake() + packet(107, agent(10))
    for split in range(len(wire) + 1):
        inspect = SpiceClientInspector()
        frames = inspect.feed(wire[:split]) + inspect.feed(wire[split:])
        assert b''.join(frame for frame, _, _ in frames) == wire
        assert frames[-1][1:] == (True, True)
    inspect = SpiceClientInspector()
    result = []
    for byte in wire:
        result += inspect.feed(bytes([byte]))
    assert result[-1][1:] == (True, True)


def test_inspector_tracks_nested_agent_fragments_and_keeps_clipboard():
    inspect = SpiceClientInspector()
    inspect.feed(handshake())
    clipboard = agent(4, b'clipboard text')
    assert inspect.feed(packet(107, clipboard))[0][1:] == (False, False)
    transfer = agent(12, b'payload')
    first = inspect.feed(packet(107, transfer[:10]))
    second = inspect.feed(packet(107, transfer[10:22]))
    third = inspect.feed(packet(107, transfer[22:]))
    assert first[0][1:] == (False, False)
    assert second[0][1:] == (True, False)
    assert third[0][1:] == (True, False)


def test_unsupported_handshake_and_agent_extensions_fail_closed():
    with pytest.raises(SpicePolicyError):
        SpiceClientInspector().feed(bytes(16))
    with pytest.raises(SpicePolicyError):
        SpiceClientInspector().feed(handshake(9))
    inspect = SpiceClientInspector()
    inspect.feed(handshake())
    with pytest.raises(SpicePolicyError):
        inspect.feed(packet(107, agent(999)))


def test_agent_restart_cannot_desynchronize_inspection():
    inspect = SpiceClientInspector()
    inspect.feed(handshake() + packet(107, agent(4, b'clipboard')[:10]))
    with pytest.raises(SpicePolicyError):
        inspect.feed(packet(106, bytes(4)))


def test_global_capabilities_are_final_authority(client, monkeypatch):
    monkeypatch.setenv('ENABLE_VM_FILE_UPLOAD', 'false')
    monkeypatch.setenv('ENABLE_VM_FILE_DOWNLOAD', 'true')
    assert client.get('/api/file-transfer/capabilities').json == {'upload': False, 'download': True}
    policy = response({'description': '{"AccessForge":{"file_upload":true,"file_download":true}}'})
    with patch.object(main, 'proxmox_get', return_value=policy):
        assert main.vm_transfer_policy('node', 101, {}, {}) == {'file_upload': False, 'file_download': True}


def test_download_checks_policy_on_every_request(client, monkeypatch):
    monkeypatch.setenv('ENABLE_VM_FILE_DOWNLOAD', 'true')
    for notes in ['', '{"AccessForge":{"file_download":false}}']:
        with patch.object(main, 'proxmox_get', side_effect=[vm_resources(), response({'description': notes})]) as get:
            assert read_request(client).status_code == 403
            assert get.call_count == 2
    with patch.object(main, 'proxmox_get', side_effect=[vm_resources(), response(None, 403)]):
        assert read_request(client).status_code == 403


def test_bridge_blocks_modified_client_uploads(live_bridge, proxy):
    from websockets.exceptions import ConnectionClosed
    with websocket(live_bridge.url) as ws:
        assert ws.recv(timeout=5) == b'server hello'
        ws.send(handshake())
        assert read_bytes(ws, len(handshake())) == handshake()
        ws.send(packet(107, agent(10)))
        with pytest.raises(ConnectionClosed) as closed:
            ws.recv(timeout=5)
        assert closed.value.rcvd.code == 1008


def test_bridge_allows_upload_then_rechecks_policy_on_next_start(live_bridge, proxy):
    from websockets.exceptions import ConnectionClosed
    grant = live_bridge.bridge.grants[live_bridge.token]
    grant.upload_allowed = True
    permitted = [True]
    grant.upload_check = lambda: permitted[0]
    with websocket(live_bridge.url) as ws:
        assert ws.recv(timeout=5) == b'server hello'
        ws.send(handshake())
        assert read_bytes(ws, len(handshake())) == handshake()
        frame = packet(107, agent(10))
        ws.send(frame)
        assert read_bytes(ws, len(frame)) == frame
        permitted[0] = False
        ws.send(frame)
        with pytest.raises(ConnectionClosed) as closed:
            ws.recv(timeout=5)
        assert closed.value.rcvd.code == 1008
