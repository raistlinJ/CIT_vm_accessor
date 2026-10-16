import test from 'node:test';
import assert from 'node:assert/strict';
import { SpiceDisplayConn } from '../static/vendor/spice-html5/src/display.js';

function displayQueue() {
  const display = Object.create(SpiceDisplayConn.prototype);
  display.ops = [];
  display.ops_head = 0;
  display.schedule_flush = () => {};
  display.cancel_flush = () => {};
  display.schedule_frame = () => {};
  return display;
}

test('draws stay ordered behind an asynchronous image decode', () => {
  const display = displayQueue();
  const drawn = [];
  display.enqueue(() => drawn.push('before'));
  const image = display.enqueue_pending();
  display.enqueue(() => drawn.push('after'));
  display.flush(Infinity);
  assert.deepEqual(drawn, ['before']);

  display.mark_ready(image, () => drawn.push('image'));
  display.flush(Infinity);
  assert.deepEqual(drawn, ['before', 'image', 'after']);
  assert.equal(display.ops.length, 0);
});

test('a large burst drains and a dropped queue does not replay old draws', () => {
  const display = displayQueue();
  let count = 0;
  for (let i = 0; i < 1000; i++) display.enqueue(() => count++);
  display.flush(Infinity);
  assert.equal(count, 1000);
  display.enqueue(() => count++);
  display.drop_queue();
  display.flush(Infinity);
  assert.equal(count, 1000);
});
