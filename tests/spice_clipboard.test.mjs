import test from 'node:test';
import assert from 'node:assert/strict';
import { AgentMessageReader, ClipboardSpiceConnection, MAX_CLIPBOARD_BYTES } from '../static/spice-clipboard.js';
import { Constants as C } from '../static/vendor/spice-html5/src/enums.js';
import { VDAgentFileXferDataMessage } from '../static/vendor/spice-html5/src/spicemsg.js';

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

test('batches file uploads while honoring agent credits and preserving packet boundaries', () => {
  const client = Object.create(ClipboardSpiceConnection.prototype);
  const writes = [];
  let observed = 0;
  Object.assign(client, {
    agent_connected: true, agent_tokens: 32, agent_msg_queue: [], messages_sent: 0,
    ws: {send: bytes => writes.push(bytes)},
    networkObserver: {record(direction, size) { assert.equal(direction, 'sent'); observed += size; }},
  });
  const payload = Uint8Array.from({length: 65536}, (_, offset) => offset & 255);
  client.send_agent_message(C.VD_AGENT_FILE_XFER_DATA, new VDAgentFileXferDataMessage(4, payload.length, payload.buffer));
  assert.equal(client.agent_tokens, 0);
  assert.equal(client.messages_sent, 32);
  assert.equal(writes.length, 1);
  assert.equal(client.agent_msg_queue.length, 1);

  client.agent_tokens = 1;
  client.send_agent_message_queue();
  assert.equal(client.agent_tokens, 0);
  assert.equal(client.agent_msg_queue.length, 0);
  assert.equal(writes.length, 2);

  const fragments = [];
  for (const write of writes) {
    const bytes = new DataView(write);
    for (let offset = 0; offset < write.byteLength;) {
      assert.equal(bytes.getUint16(offset, true), C.SPICE_MSGC_MAIN_AGENT_DATA);
      const size = bytes.getUint32(offset + 2, true);
      assert.ok(size <= C.VD_AGENT_MAX_DATA_SIZE);
      fragments.push(new Uint8Array(write, offset + 6, size));
      offset += 6 + size;
    }
  }
  assert.equal(fragments.length, 33);
  assert.equal(observed, writes.reduce((total, write) => total + write.byteLength, 0));
  assert.deepEqual(concat(...fragments), agent(C.VD_AGENT_FILE_XFER_DATA,
    concat(u32(4, payload.length, 0), payload)));
});

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
  assert.throws(() => new AgentMessageReader().push(u32(1, 2, 0, 0, MAX_CLIPBOARD_BYTES + 65).buffer), /oversized/);
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
  assert.equal(client.guestOwnsClipboard, false);
  client.inject(8, u32(0, 2));
  assert.deepEqual(client.sent.at(-1).bytes, u32(0, 0));
});

test('a rejected guest clipboard message leaves the connection ready for a later copy', () => {
  const client = peer(false);
  const errors = [];
  client.onclipboarderror = message => errors.push(message);
  client.inject(7, new Uint8Array([1])); // malformed format list
  assert.match(errors.at(-1), /invalid clipboard format/);
  client.inject(7, u32(1));
  const oversized = agent(4, concat(u32(1), new Uint8Array(MAX_CLIPBOARD_BYTES + 100)));
  for (let position = 0; position < oversized.length; position += 2048) {
    client.process_channel_message({type: 109, data: oversized.slice(position, position + 2048).buffer});
  }
  assert.match(errors.at(-1), /exceeds 1 MiB/);
  assert.equal(client.awaitingClipboard, false);
  assert.equal(client.agentReader.skip, 0);
  client.inject(7, u32(1));
  client.inject(4, concat(u32(1), utf8.encode('hello from Windows')));
  assert.equal(client.received.at(-1), 'hello from Windows');
  assert.equal(client.clipboardReady, true);
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

test('limited VM clipboard waits for an explicit receive action', () => {
  const client = peer();
  client.clipboardPolicy = {from_vm: {enabled: true, limit: 100, remaining: 100}};
  client.inject(7, u32(0, 1));
  assert.equal(client.sent.length, 0);
  assert.equal(client.awaitingClipboard, false);
  client.requestClipboard();
  assert.equal(client.sent[0].kind, 8);
  assert.equal(client.awaitingClipboard, true);
  client.inject(4, concat(u32(0, 1), utf8.encode('received')));
  assert.equal(client.received.at(-1), 'received');
});

test('client allowance counts Unicode characters and disabled directions cannot share', () => {
  const client = peer();
  client.clipboardPolicy = {to_vm: {enabled: true, limit: 2, remaining: 2}, from_vm: {enabled: false, remaining: 0}};
  assert.equal(client.offerClipboard('é😀'), true);
  assert.equal(client.offerClipboard('é😀x'), false);
  assert.equal(client.sent.length, 1);
  client.inject(7, u32(0, 1));
  const sent = client.sent.length;
  client.requestClipboard();
  assert.equal(client.sent.length, sent);
  client.clipboardPolicy.to_vm.enabled = false;
  assert.throws(() => client.offerClipboard('x'), /disabled/);
});
