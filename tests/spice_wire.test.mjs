import test from 'node:test';
import assert from 'node:assert/strict';
import { SpiceWireReader } from '../static/vendor/spice-html5/src/wire.js';

function packet(...bytes) {
  return Uint8Array.from(bytes).buffer;
}

test('wire reader preserves header and body order across WebSocket boundaries', () => {
  const ws = { addEventListener() {} };
  const received = [];
  const sc = { ws };
  let reader;
  reader = new SpiceWireReader(sc, (frame, header) => {
    const bytes = [...new Uint8Array(frame)];
    if (header === undefined) {
      const next = { id: bytes[0], length: bytes[1] };
      reader.save_header(next);
      reader.request(next.length);
    } else {
      received.push({ header, bytes });
      reader.clear_header();
      reader.request(2);
    }
  });
  reader.request(2);

  reader.inbound(packet(1));
  reader.inbound(packet(3, 10, 11));
  reader.inbound(packet(12, 2, 2, 20, 21, 3));
  reader.inbound(packet(1, 30));

  assert.deepEqual(received, [
    { header: { id: 1, length: 3 }, bytes: [10, 11, 12] },
    { header: { id: 2, length: 2 }, bytes: [20, 21] },
    { header: { id: 3, length: 1 }, bytes: [30] },
  ]);
  assert.equal(reader.size, 0);
});

test('wire reader handles many complete messages in one WebSocket packet', () => {
  const sc = { ws: { addEventListener() {} } };
  const received = [];
  let reader;
  reader = new SpiceWireReader(sc, frame => {
    received.push(new Uint8Array(frame)[0]);
    reader.request(1);
  });
  reader.request(1);
  reader.inbound(Uint8Array.from({ length: 1000 }, (_, i) => i % 256).buffer);

  assert.equal(received.length, 1000);
  assert.equal(received[999], 999 % 256);
  assert.equal(reader.size, 0);
});
