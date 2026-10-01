import test from 'node:test';
import assert from 'node:assert/strict';
import { AgentMessageReader, ClipboardSpiceConnection, MAX_CLIPBOARD_BYTES } from '../static/spice-clipboard.js';

const utf8 = new TextEncoder();
const decode = new TextDecoder();
function u32(...values) {
  const bytes = new Uint8Array(values.length * 4);
  values.forEach((value, i) => new DataView(bytes.buffer).setUint32(i * 4, value, true));
  return bytes;
}
function concat(...chunks) {
  const result = new Uint8Array(chunks.reduce((size, chunk) => size + chunk.byteLength, 0));
  let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; }
  return result;
}
function agent(kind, body) { return concat(u32(1, kind, 0, 0, body.length), body); }
function peer(selection = true) {
  const client = Object.create(ClipboardSpiceConnection.prototype);
  Object.assign(client, {
    agent_connected: true, agent_caps: [selection ? 96 : 32],
    agentReader: new AgentMessageReader(), localClipboard: null,
    guestOwnsClipboard: false, awaitingClipboard: false,
    sent: [], received: [], states: [],
    onclipboard(text) { this.received.push(text); },
    onclipboardstate(ready) { this.states.push(ready); },
    send_agent_message(kind, message) {
      const buffer = new ArrayBuffer(message.buffer_size());
      message.to_buffer(buffer);
      this.sent.push({ kind, bytes: new Uint8Array(buffer) });
    },
    report_error(error) { throw error; },
  });
  client.inject = (kind, body) => client.process_channel_message({ type: 109, data: agent(kind, body).buffer });
  return client;
}

test('reassembles split headers, Unicode payloads, empty bodies, and coalesced messages', () => {
  const messages = [agent(6, u32(0, 96)), agent(9, new Uint8Array()), agent(4, concat(u32(0, 1), utf8.encode('é😀\n'.repeat(2000))))];
  const stream = concat(...messages);
  for (const chunkSize of [1, 7, 2048, stream.length]) {
    const reader = new AgentMessageReader();
    const decoded = [];
    for (let i = 0; i < stream.length; i += chunkSize) decoded.push(...reader.push(stream.slice(i, i + chunkSize).buffer));
    assert.deepEqual(decoded.map(data => new Uint8Array(data)), messages);
    assert.equal(reader.message, null);
    assert.equal(reader.headerUsed, 0);
  }
});

test('rejects oversized agent messages and invalid protocol before allocation', () => {
  assert.throws(() => new AgentMessageReader().push(u32(1, 4, 0, 0, MAX_CLIPBOARD_BYTES + 65).buffer), /oversized/);
  assert.throws(() => new AgentMessageReader().push(u32(2, 4, 0, 0, 0).buffer), /invalid/);
});

for (const selection of [true, false]) {
  test(`shares Unicode text in both directions with selection capability ${selection}`, () => {
    const client = peer(selection);
    const prefix = selection ? u32(0) : new Uint8Array();
    const type = concat(prefix, u32(1));
    const text = 'Hello café 😀\nこんにちは';
    client.offerClipboard(text);
    assert.equal(client.sent[0].kind, 7);
    assert.deepEqual(client.sent[0].bytes, type);
    client.inject(8, type);
    assert.equal(client.sent[1].kind, 4);
    assert.equal(decode.decode(client.sent[1].bytes.slice(type.length)), text);
    client.inject(7, type);
    assert.equal(client.localClipboard, null);
    assert.equal(client.sent[2].kind, 8);
    client.inject(4, concat(type, utf8.encode(text)));
    assert.equal(client.received.at(-1), text);
  });
}

test('never reads system clipboard on display focus or an unsolicited guest request', () => {
  const client = peer();
  client.send_clipboard_grab();
  assert.equal(client.sent.length, 0);
  client.inject(8, u32(0, 1));
  assert.deepEqual(client.sent[0].bytes, u32(0, 0));
  client.inject(4, concat(u32(0, 1), utf8.encode('unsolicited')));
  assert.equal(client.received.length, 0);
});

test('preserves empty clipboard updates and enforces a UTF-8 byte limit', () => {
  const client = peer();
  client.offerClipboard('');
  client.inject(8, u32(0, 1));
  assert.deepEqual(client.sent[1].bytes, u32(0, 1));
  assert.throws(() => client.offerClipboard('é'.repeat(MAX_CLIPBOARD_BYTES / 2 + 1)), /1 MiB/);
  client.agent_connected = false;
  assert.throws(() => client.offerClipboard('hello'), /guest agent/);
});

test('ignores Linux PRIMARY and declines non-text clipboard formats', () => {
  const client = peer();
  client.offerClipboard('local');
  client.inject(7, u32(1, 1)); // PRIMARY selection
  assert.equal(client.localClipboard, 'local');
  assert.equal(client.sent.length, 1);
  client.inject(7, u32(0, 2)); // PNG
  assert.equal(client.localClipboard, null);
  assert.equal(client.sent.length, 1);
  assert.equal(client.awaitingClipboard, false);
  client.inject(8, u32(0, 2));
  assert.deepEqual(client.sent.at(-1).bytes, u32(0, 0));
});

test('clipboard release and agent disconnect clear cached text and pending requests', () => {
  const client = peer();
  client.inject(7, u32(0, 1));
  client.inject(4, concat(u32(0, 1), utf8.encode('guest secret')));
  client.inject(9, u32(0));
  assert.equal(client.received.at(-1), null);
  assert.equal(client.guestOwnsClipboard, false);
  client.offerClipboard('local secret');
  client.process_channel_message({ type: 108, data: new ArrayBuffer(0) });
  assert.equal(client.localClipboard, null);
  assert.equal(client.clipboardReady, false);
  assert.equal(client.received.at(-1), null);
  assert.equal(client.states.at(-1), false);
});
