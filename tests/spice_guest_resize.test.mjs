import test from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as wait } from 'node:timers/promises';
import { GuestResize } from '../static/spice-guest-resize.js';

test('window changes send a new size and ignored requests retry at most three times', async () => {
  let area = { width: 801, height: 601 };
  const sent = [];
  const resize = new GuestResize({
    area: () => area,
    surface: () => ({ width: 640, height: 480 }),
    connected: () => true,
    send: (width, height) => sent.push([width, height]),
    retryDelay: 10,
  });
  try {
    resize.request();
    resize.request(); // repeated layout notifications must not spend a retry
    assert.deepEqual(sent, [[800, 600]]);
    await wait(40);
    assert.deepEqual(sent, [[800, 600], [800, 600], [800, 600]]);
    area = { width: 1100, height: 760 };
    resize.request();
    assert.deepEqual(sent.at(-1), [1096, 760]);
  } finally {
    resize.reset();
  }
});

test('matching guest surface and disconnect stop pending retries', async () => {
  let surface = { width: 640, height: 480 };
  let connected = true;
  const sent = [];
  const resize = new GuestResize({
    area: () => ({ width: 801, height: 601 }),
    surface: () => surface,
    connected: () => connected,
    send: (width, height) => sent.push([width, height]),
    retryDelay: 10,
  });
  try {
    resize.request();
    surface = { width: 800, height: 600 };
    await wait(25);
    assert.equal(sent.length, 1);
    resize.reset();
    connected = false;
    resize.request();
    assert.equal(sent.length, 1);
  } finally {
    resize.reset();
  }
});
