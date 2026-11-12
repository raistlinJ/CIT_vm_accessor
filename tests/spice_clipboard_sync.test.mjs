import test from 'node:test';
import assert from 'node:assert/strict';
import { ClipboardSync, clipboardAccessHelp } from '../static/spice-clipboard-sync.js';

test('explains Safari and Firefox restrictions instead of offering unsupported automatic sharing', () => {
  for (const userAgent of ['Mozilla/5.0 Version/18.0 Safari/605.1.15', 'Mozilla/5.0 Firefox/140.0']) {
    const result = clipboardAccessHelp({ userAgent, secure: true, readable: true, writable: true });
    assert.equal(result.automaticSupported, false);
    assert.match(result.message, /no always-allow clipboard setting/);
    assert.match(result.message, /Paste from computer/);
    assert.match(result.message, /Chrome or Edge/);
  }
});

test('provides Chrome settings instructions and distinguishes HTTPS or missing API problems', () => {
  const options = { userAgent: 'Mozilla/5.0 Chrome/140.0 Safari/537.36', secure: true, readable: true, writable: true };
  const chrome = clipboardAccessHelp(options);
  assert.equal(chrome.automaticSupported, true);
  assert.match(chrome.message, /Site settings → Clipboard → Allow/);
  assert.match(chrome.message, /popup hides the address bar/);
  assert.match(chrome.url, /support.google.com/);
  const insecure = clipboardAccessHelp({ ...options, secure: false });
  assert.equal(insecure.automaticSupported, false);
  assert.match(insecure.message, /HTTPS/);
  const missing = clipboardAccessHelp({ ...options, readable: false });
  assert.equal(missing.automaticSupported, false);
  assert.match(missing.message, /unavailable/);
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function setup() {
  const state = { active: true, text: 'computer', reads: 0, writes: [], offers: [], errors: [] };
  const sync = new ClipboardSync({
    readText() { state.reads++; return Promise.resolve(state.text); },
    writeText(text) { state.writes.push(text); state.text = text; return Promise.resolve(); },
    offerText(text) { state.offers.push(text); sync.receiveGuest(null); },
    isActive: () => state.active,
    onSend() {}, onCopy() {},
    onError: (direction, error) => state.errors.push([direction, error.message]),
  });
  return { sync, state };
}

test('starts off, requires an active console, and sends only changed text including empty text', async () => {
  const { sync, state } = setup();
  await sync.poll();
  await sync.receiveGuest('guest');
  assert.equal(state.reads, 0);
  assert.deepEqual(state.writes, []);
  state.active = false;
  await sync.setToVM(true);
  assert.equal(state.reads, 0);
  state.active = true;
  await sync.poll();
  await sync.poll();
  assert.deepEqual(state.offers, ['computer']);
  state.text = '';
  await sync.poll();
  assert.deepEqual(state.offers, ['computer', '']);
  await sync.setToVM(false);
  state.text = 'not shared';
  await sync.poll();
  assert.equal(state.offers.length, 2);
});

test('each direction works independently and guest writes are not echoed back', async () => {
  const { sync, state } = setup();
  await sync.receiveGuest('guest 😀');
  await sync.setFromVM(true);
  assert.deepEqual(state.writes, ['guest 😀']);
  assert.equal(state.reads, 0);
  await sync.setToVM(true);
  assert.deepEqual(state.offers, []);
  await sync.receiveGuest('another guest copy');
  await sync.poll();
  assert.deepEqual(state.offers, []);
  await sync.setFromVM(false);
  await sync.receiveGuest('not copied');
  assert.equal(state.writes.length, 2);
});

test('superseded reads cannot overwrite a newer guest copy', async () => {
  const { sync, state } = setup();
  const read = deferred();
  sync.readText = () => read.promise;
  const pull = sync.setToVM(true);
  await sync.setFromVM(true);
  await sync.receiveGuest('new guest text');
  read.resolve('stale computer text');
  await pull;
  assert.deepEqual(state.offers, []);
  assert.deepEqual(state.writes, ['new guest text']);
});

test('overlapping polls share one read and disabling or resetting invalidates it', async () => {
  const { sync, state } = setup();
  let reads = 0;
  let read = deferred();
  sync.readText = () => { reads++; return read.promise; };
  const first = sync.setToVM(true);
  await sync.poll();
  assert.equal(reads, 1);
  sync.setToVM(false);
  read.resolve('disabled');
  await first;
  read = deferred();
  const second = sync.setToVM(true);
  sync.reset();
  read.resolve('old connection');
  await second;
  assert.deepEqual(state.offers, []);
  assert.equal(sync.toVM, false);
  assert.equal(sync.fromVM, false);
  assert.equal(sync.hostText, null);
});

test('permission failures stop only the affected direction', async () => {
  const { sync, state } = setup();
  sync.readText = async () => { throw new Error('read denied'); };
  await sync.setFromVM(true);
  await sync.setToVM(true);
  assert.equal(sync.toVM, false);
  assert.equal(sync.fromVM, true);
  sync.writeText = async () => { throw new Error('write denied'); };
  await sync.receiveGuest('guest');
  assert.equal(sync.fromVM, false);
  assert.deepEqual(state.errors, [['to', 'read denied'], ['from', 'write denied']]);
});

test('serializes guest writes, coalesces updates, and suspends reads until they finish', async () => {
  const { sync, state } = setup();
  const write = deferred();
  sync.writeText = text => {
    state.writes.push(text);
    return state.writes.length === 1 ? write.promise : Promise.resolve();
  };
  await sync.setFromVM(true);
  const first = sync.receiveGuest('first');
  await sync.setToVM(true);
  await sync.receiveGuest('middle');
  await sync.receiveGuest('latest');
  assert.equal(state.reads, 0);
  assert.deepEqual(state.writes, ['first']);
  write.resolve();
  await first;
  await Promise.resolve();
  assert.deepEqual(state.writes, ['first', 'latest']);
  assert.equal(sync.hostText, 'latest');
});

test('copies deferred guest updates on focus and the same text after ownership changes', async () => {
  const { sync, state } = setup();
  await sync.setFromVM(true);
  state.active = false;
  await sync.receiveGuest('guest');
  assert.deepEqual(state.writes, []);
  state.active = true;
  await sync.copyGuest();
  await sync.receiveGuest(null);
  await sync.receiveGuest('guest');
  assert.deepEqual(state.writes, ['guest', 'guest']);
});

test('reset prevents a delayed write from scheduling another write or restoring text', async () => {
  const { sync, state } = setup();
  const write = deferred();
  sync.writeText = text => { state.writes.push(text); return write.promise; };
  await sync.setFromVM(true);
  const pending = sync.receiveGuest('old guest');
  await sync.receiveGuest('queued old guest');
  sync.reset();
  write.resolve();
  await pending;
  assert.deepEqual(state.writes, ['old guest']);
  assert.equal(sync.hostText, null);
  assert.equal(sync.guestText, null);
});
