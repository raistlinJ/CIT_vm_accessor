import test from 'node:test';
import assert from 'node:assert/strict';
import { ConsoleNetworkMetrics, formatNetworkBytes } from '../static/spice-network.js';

test('disabled metrics have no observer; enabling starts a fresh measurement', () => {
  const metrics = new ConsoleNetworkMetrics();
  assert.equal(metrics.observer.record, null);
  metrics.enable(true, 0);
  metrics.observer.record('received', 2048);
  metrics.observer.record('sent', 512);
  assert.deepEqual(metrics.sample(2000), {receivedRate: 1024, sentRate: 256, received: 2048, sent: 512});
  assert.equal(metrics.sample(3000).receivedRate, 0);
  metrics.enable(false, 3000);
  assert.equal(metrics.observer.record, null);
  metrics.enable(true, 4000);
  assert.equal(metrics.sample(5000).received, 0);
});

test('shared channel observer survives connection resets and uses elapsed time', () => {
  const metrics = new ConsoleNetworkMetrics();
  const main = metrics.observer, display = metrics.observer;
  metrics.enable(true, 0);
  main.record('received', 100);
  display.record('received', 200);
  assert.equal(metrics.sample(100).receivedRate, 3000);
  metrics.reset(100);
  display.record('sent', 500);
  assert.deepEqual(metrics.sample(1100), {receivedRate: 0, sentRate: 500, received: 0, sent: 500});
  assert.equal(metrics.sample(1100).sentRate, 0);
});

test('network units use bytes and binary scaling', () => {
  assert.equal(formatNetworkBytes(0), '0 B');
  assert.equal(formatNetworkBytes(1024), '1.0 KiB');
  assert.equal(formatNetworkBytes(1572864), '1.5 MiB');
});
