from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import struct
from unittest.mock import patch

import pytest
from clipboard_store import ClipboardStore
from clipboard_policy import parse_clipboard_policy
from clipboard_wire import ClipboardWire, packet
import main
from test_spice import client, response
from test_vm_files import vm_resources
from spice_bridge import SpiceBridge, Grant
from test_transfer_policy import handshake


def policy(**changes):
    return parse_clipboard_policy({'clipboard_id': 'shared-vm', 'clipboard_to_vm': True,
        'clipboard_from_vm': True, 'clipboard_to_vm_limit': 5, 'clipboard_from_vm_limit': 8, **changes})


@pytest.mark.parametrize('metadata', [None, {}, {'materials': []}])
def test_unconfigured_clipboard_defaults_to_unlimited_in_both_directions(metadata):
    for rule in parse_clipboard_policy(metadata).values():
        assert rule['enabled'] is True
        assert rule['limit'] is None


@pytest.mark.parametrize('notes', [None, '', 'Human-readable notes only', '{"Scenario":"lab"}',
                                  '{"AccessForge":{}}', '{"AccessForge":{"file_upload":false}}'])
def test_vm_without_clipboard_settings_allows_both_directions(client, tmp_path, monkeypatch, notes):
    monkeypatch.setitem(main.app.config, 'CHECKOUT_DATA_DIR', str(tmp_path))
    with patch.object(main, 'proxmox_get', side_effect=[vm_resources(), response({'description': notes})]):
        result = client.get('/api/spice/101/clipboard')
    assert result.status_code == 200
    for rule in result.json['clipboard'].values():
        assert rule['enabled'] is True and rule['limit'] is None and rule['remaining'] is None


@pytest.mark.parametrize('notes', ['{"AccessForge":false}', '{"AccessForge":{broken',
    '{"AccessForge":{"clipboard_to_vm":false,"clipboard_to_vm":true}}',
    '{"AccessForge":{}}\n{"AccessForge":{}}'])
def test_malformed_clipboard_settings_remain_unavailable(client, tmp_path, monkeypatch, notes):
    monkeypatch.setitem(main.app.config, 'CHECKOUT_DATA_DIR', str(tmp_path))
    with patch.object(main, 'proxmox_get', side_effect=[vm_resources(), response({'description': notes})]):
        assert client.get('/api/spice/101/clipboard').status_code == 503


def test_explicit_disabled_direction_and_its_limit_are_preserved():
    policy = parse_clipboard_policy({'clipboard_to_vm': False, 'clipboard_from_vm_limit': 50})
    assert policy['to_vm']['enabled'] is False
    assert policy['from_vm']['enabled'] is True and policy['from_vm']['limit'] == 50


def test_counts_are_atomic_persistent_independent_and_shared_across_logins(tmp_path):
    rule = policy()
    store = ClipboardStore(tmp_path)
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: ClipboardStore(tmp_path).consume('cluster', 101, rule, [('to_vm', 1)]), range(20)))
    assert sum(results) == 5
    assert store.snapshot('other-api-host-same-vm', 101, rule)['to_vm']['remaining'] == 0
    assert store.consume('cluster', 101, rule, [('from_vm', 3)])
    assert store.snapshot('cluster', 101, rule)['from_vm']['remaining'] == 5
    assert store.snapshot('cluster', 102, rule)['to_vm']['used'] == 0


def test_reset_and_limit_changes_do_not_restore_old_counts(tmp_path):
    store = ClipboardStore(tmp_path)
    rule = policy()
    assert store.consume('cluster', 101, rule, [('to_vm', 5)])
    changed = policy(clipboard_to_vm_limit=20)
    assert store.snapshot('cluster', 101, changed)['to_vm']['used'] == 5
    reset = policy(clipboard_to_vm_reset='new-reset', clipboard_from_vm_reset='new-reset')
    assert store.snapshot('cluster', 101, reset)['to_vm']['used'] == 0
    assert store.consume('cluster', 101, reset, [('to_vm', 5)])
    assert not store.consume('cluster', 101, reset, [('to_vm', 1)])


def test_rejected_batch_charges_neither_direction(tmp_path):
    store = ClipboardStore(tmp_path)
    assert not store.consume('cluster', 101, policy(), [('to_vm', 2), ('from_vm', 9)])
    assert store.snapshot('cluster', 101, policy())['to_vm']['used'] == 0
    assert store.snapshot('cluster', 101, policy())['from_vm']['denied_at']
    assert not store.consume('cluster', 101, policy(clipboard_to_vm=False), [('to_vm', 0)])


def peers():
    client, server = ClipboardWire(), ClipboardWire(server=True)
    client.peer, server.peer = server, client
    client.stage = server.stage = 'messages'
    client.caps = server.caps = 64
    server.requested = True
    return client, server


def agent(kind, body):
    return struct.pack('<IIQI', 1, kind, 0, len(body)) + body


@pytest.mark.parametrize('server,direction', [(False, 'to_vm'), (True, 'from_vm')])
def test_wire_waits_for_complete_unicode_text_and_preserves_fragments(server, direction):
    client, guest = peers()
    wire = guest if server else client
    raw = agent(4, struct.pack('<II', 0, 1)+'é😀\n'.encode())
    kind = 109 if server else 107
    frames = [packet(kind, raw[:12]), packet(kind, raw[12:24]), packet(kind, raw[24:])]
    credits = []
    for frame in frames[:-1]:
        ready, credit, events = wire.prepare(frame, kind)
        assert not ready and events is None
        credits.append(credit)
    _, credit, events = wire.prepare(frames[-1], kind)
    assert events == [(direction, 3)]
    assert wire.finish(True) == (frames, None)
    assert wire.debt == 2
    token_kind = 108 if server else 110
    ready, _, _ = wire.peer.prepare(packet(token_kind, struct.pack('<I', 3)), token_kind)
    assert ready == [packet(token_kind, struct.pack('<I', 1))]
    assert wire.debt == 0


def test_wire_rejects_without_forwarding_text_and_reimburses_all_tokens():
    client, server = peers()
    raw = agent(4, struct.pack('<II', 0, 1)+b'private text')
    assert client.prepare(packet(107, raw[:21]), 107)[1]
    assert client.prepare(packet(107, raw[21:]), 107)[2] == [('to_vm', 12)]
    frames, credit = client.finish(False)
    assert not frames and credit == packet(110, struct.pack('<I', 1))
    assert client.debt == 0
    # A rejected transfer must not prevent subsequent permitted messages.
    raw = agent(4, struct.pack('<II', 0, 1)+b'ok')
    assert client.prepare(packet(107, raw), 107)[2] == [('to_vm', 2)]
    assert client.finish(True)[0] == [packet(107, raw)]


def test_wire_directions_and_primary_selection():
    client, server = peers()
    for wire, kind, expected in ((client, 107, 'from_vm'), (server, 109, 'to_vm')):
        raw = agent(8, struct.pack('<II', 0, 1))
        assert wire.prepare(packet(kind, raw), kind)[2] == [(expected, 0)]
        wire.finish(True)
    raw = agent(4, struct.pack('<II', 1, 1)+b'primary')
    assert server.prepare(packet(109, raw), 109)[2] == [('from_vm', -1)]


def test_unsolicited_vm_text_cannot_spend_allowance_until_client_requests_it():
    client, server = peers()
    server.requested = False
    raw = agent(4, struct.pack('<II', 0, 1)+b'unsolicited')
    assert server.prepare(packet(109, raw), 109)[2] == [('from_vm', -1)]
    server.finish(False)
    request = packet(107, agent(8, struct.pack('<II', 0, 1)))
    client.prepare(request, 107)
    client.finish(True)
    assert server.requested
    assert server.prepare(packet(109, raw), 109)[2] == [('from_vm', 11)]


def test_admin_usage_requires_admin_auth_and_reports_current_policy(tmp_path, monkeypatch):
    monkeypatch.setitem(main.app.config, 'CHECKOUT_DATA_DIR', str(tmp_path))
    notes = json.dumps({'AccessForge': {'clipboard_id': 'shared-vm', 'clipboard_to_vm_limit': 5}})
    assert ClipboardStore(tmp_path).consume('cluster', 101, policy(), [('to_vm', 3)])
    with main.app.test_client() as client:
        assert client.get('/api/admin/clipboard/usage?vmids=101').status_code == 401
        with patch('account_checkout.authenticate_admin', return_value={'ticket': 'admin', 'username': 'root@pam'}), \
             patch.object(main.requests, 'get', side_effect=[vm_resources(), response({'description': notes})]):
            result = client.get('/api/admin/clipboard/usage?vmids=101')
        assert result.status_code == 200
        assert result.json['usage']['101']['to_vm']['remaining'] == 2


def test_bridge_enforces_both_directions_without_closing_console(tmp_path):
    async def run():
        loop = asyncio.get_running_loop()
        checked = asyncio.Queue()
        store = ClipboardStore(tmp_path)
        rule = policy()
        def authorize(events):
            allowed = store.consume('cluster', 101, rule, events)
            loop.call_soon_threadsafe(checked.put_nowait, (events, allowed))
            return allowed
        class Browser:
            request = None
            def __init__(self):
                self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
                self.closed = False
            def __aiter__(self): return self
            async def __anext__(self): return await self.incoming.get()
            async def send(self, data): self.outgoing.put_nowait(data)
            async def close(self, *args): self.closed = True
        class Reader:
            def __init__(self): self.incoming = asyncio.Queue()
            async def read(self, size): return await self.incoming.get()
        class Writer:
            def __init__(self): self.outgoing = asyncio.Queue()
            def write(self, data): self.outgoing.put_nowait(data)
            async def drain(self): pass
            def close(self): pass
            async def wait_closed(self): pass
        browser, reader, writer = Browser(), Reader(), Writer()
        bridge = SpiceBridge(lambda _: 'owner')
        grant = Grant(None, 'owner', 'origin', 0, clipboard_check=authorize)
        async def tunnel(target): return reader, writer
        async def take(queue): return await asyncio.wait_for(queue.get(), 3)
        with patch.object(bridge, '_lookup', return_value=grant), patch('spice_bridge.open_tunnel', tunnel):
            task = asyncio.create_task(bridge.relay(browser))
            try:
                browser.incoming.put_nowait(handshake())
                await take(writer.outgoing); await take(writer.outgoing)
                reply = struct.pack('<4sIII', b'REDQ', 2, 2, 22)+bytes(22)+bytes(4)
                reader.incoming.put_nowait(reply)
                await take(browser.outgoing); await take(browser.outgoing)
                caps = agent(6, struct.pack('<II', 0, 64))
                browser.incoming.put_nowait(packet(107, caps)); await take(writer.outgoing)
                reader.incoming.put_nowait(packet(109, caps)); await take(browser.outgoing)
                # Allow exactly five characters across fragmented client packets.
                body = agent(4, struct.pack('<II', 0, 1)+b'hello')
                browser.incoming.put_nowait(packet(107, body[:22]))
                assert await take(browser.outgoing) == packet(110, struct.pack('<I', 1))
                assert writer.outgoing.empty()
                browser.incoming.put_nowait(packet(107, body[22:]))
                assert await take(checked) == ([('to_vm', 5)], True)
                await take(writer.outgoing); await take(writer.outgoing)
                # Further client text is dropped; the display stays connected.
                blocked = packet(107, agent(4, struct.pack('<II', 0, 1)+b'x'))
                browser.incoming.put_nowait(blocked)
                assert await take(checked) == ([('to_vm', 1)], False)
                assert await take(browser.outgoing) == packet(110, struct.pack('<I', 1))
                assert writer.outgoing.empty()
                # An explicit VM receive request permits the reverse transfer.
                request = packet(107, agent(8, struct.pack('<II', 0, 1)))
                browser.incoming.put_nowait(request)
                assert await take(checked) == ([('from_vm', 0)], True)
                await take(writer.outgoing)
                reverse = packet(109, agent(4, struct.pack('<II', 0, 1)+b'eight!!!'))
                reader.incoming.put_nowait(reverse)
                assert await take(checked) == ([('from_vm', 8)], True)
                assert await take(browser.outgoing) == reverse
                assert not browser.closed
                assert store.snapshot('cluster', 101, rule)['to_vm']['remaining'] == 0
                assert store.snapshot('cluster', 101, rule)['from_vm']['remaining'] == 0
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_wire_handshakes_survive_every_byte_boundary():
    for is_server in (False, True):
        wire = ClipboardWire(server=is_server)
        wire.peer = ClipboardWire(server=not is_server)
        body = bytes(22 if is_server else 18)
        header = struct.pack('<4sIII', b'REDQ', 2, 2, len(body))
        stream = header+body+bytes(4 if is_server else 132)+packet(4, b'normal')
        output = []
        for value in stream:
            for frame, kind in wire.feed(bytes([value])):
                output += wire.prepare(frame, kind)[0]
        assert b''.join(output) == stream


def test_client_usage_authorizes_vm_and_reads_persistent_counts(client, tmp_path, monkeypatch):
    monkeypatch.setitem(main.app.config, 'CHECKOUT_DATA_DIR', str(tmp_path))
    notes = json.dumps({'AccessForge': {'clipboard_to_vm': True, 'clipboard_to_vm_limit': 20}})
    with patch.object(main, 'proxmox_get', side_effect=[vm_resources(), response({'description': notes})]):
        result = client.get('/api/spice/101/clipboard')
    assert result.status_code == 200 and result.json['clipboard']['to_vm']['remaining'] == 20
    assert result.headers['Cache-Control'] == 'no-store'
    with patch.object(main, 'proxmox_get', return_value=response([], 403)):
        assert client.get('/api/spice/101/clipboard').status_code == 503
