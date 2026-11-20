const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/spice-console.js', 'utf8');
const helpers = source.slice(source.indexOf('function clipboardLimited()'), source.indexOf('const MAX_VM_FILE_BYTES ='));

test('either limit turns off both automatic switches and displays both persistent remainders', () => {
  const box = () => ({checked:true,disabled:false,value:''});
  const context = vm.createContext({
    connection:{clipboardReady:true,guestOwnsClipboard:true,awaitingClipboard:false},
    clipboardPolicy:{to_vm:{enabled:true,limit:20,remaining:12,used:8},from_vm:{enabled:true,limit:null,remaining:null,used:5}},
    clipboardSend:box(),clipboardSendButton:box(),clipboardReceiveButton:box(),clipboardAutoTo:box(),clipboardAutoFrom:box(),
    clipboardSendPending: false,
    clipboardExceedsAllowance: () => false,
    clipboardAccess:{automaticSupported:true},clipboardReceive:box(),clipboardCopy:box(),clipboardRemaining:{},
    clipboardPolicyStatus:{dataset:{}},
    clipboardSync:{setToVM: enabled=>assert.equal(enabled,false),setFromVM: enabled=>assert.equal(enabled,false)},
    updateClipboardSyncTimer(){},
  });
  // Only the policy functions are needed; refresh itself is not called here.
  vm.runInContext(helpers,context);
  context.applyClipboardPolicy();
  assert.equal(context.clipboardAutoTo.disabled,true);
  assert.equal(context.clipboardAutoFrom.disabled,true);
  assert.equal(context.clipboardAutoTo.checked,false);
  assert.equal(context.clipboardAutoFrom.checked,false);
  assert.match(context.clipboardRemaining.innerHTML, /Client → VM<\/th><td>8 \/ 20<\/td>.*data-band="medium">12 <small>60%/);
  assert.match(context.clipboardRemaining.innerHTML, /VM → client<\/th><td>—<\/td><td>Unlimited/);
  assert.equal(context.clipboardReceiveButton.disabled,false);
});

function availability(policy, ready = true, automaticSupported = true) {
  const context = vm.createContext({
    clipboardPolicy: policy, connection: {clipboardReady: ready},
    clipboardAccess: {automaticSupported}, clipboardPolicyStatus: {dataset: {}},
  });
  vm.runInContext(helpers, context);
  context.updateClipboardAvailability();
  return context.clipboardPolicyStatus;
}
const allowed = {enabled: true, limit: null, remaining: null};

test('a manual allowance check and a background refresh share the same result', async () => {
  let finishFetch;
  let requests = 0;
  const context = vm.createContext({
    AbortController,
    clipboardUsageRequest: null,
    clipboardPolicy: null,
    document: {body: {dataset: {vmid: '101'}}},
    fetch() {
      requests++;
      return new Promise(resolve => { finishFetch = resolve; });
    },
  });
  vm.runInContext(helpers, context);
  context.applyClipboardPolicy = () => {};
  const manual = context.refreshClipboardUsage();
  const poll = context.refreshClipboardUsage();
  assert.equal(requests, 1);
  finishFetch({ok: true, json: async () => ({clipboard: {to_vm: allowed, from_vm: allowed}})});
  assert.deepEqual(await Promise.all([manual, poll]), [true, true]);
  assert.equal(context.clipboardUsageRequest, null);

  const failure = context.refreshClipboardUsage();
  finishFetch({ok: false, json: async () => ({})});
  assert.equal(await failure, false);
  assert.equal(context.clipboardPolicy, null);
  const retry = context.refreshClipboardUsage();
  finishFetch({ok: true, json: async () => ({clipboard: {to_vm: allowed, from_vm: allowed}})});
  assert.equal(await retry, true);
});

test('allowance colors follow the remaining percentage, including exact thresholds and zero allowance', () => {
  const context = vm.createContext({clipboardRemaining: {}, clipboardPolicy: {from_vm: {...allowed, enabled: false}}});
  vm.runInContext(helpers, context);
  for (const [limit, remaining, band] of [
    [2000, 2000, 'high'], [2000, 1501, 'high'], [2000, 1500, 'medium'],
    [2000, 600, 'medium'], [2000, 599, 'low'], [0, 0, 'low'],
  ]) {
    context.clipboardPolicy.to_vm = {enabled: true, limit, remaining, used: limit - remaining};
    context.renderClipboardAllowances();
    assert.ok(context.clipboardRemaining.innerHTML.includes(`data-band="${band}"`));
    assert.match(context.clipboardRemaining.innerHTML, /VM → client<\/th><td>—<\/td><td>Disabled/);
  }
});

test('the popup clearly names disabled directions without saying they are ready', () => {
  const status = availability({to_vm: {...allowed, enabled: false}, from_vm: allowed});
  assert.match(status.textContent, /Client → VM clipboard is disabled by this VM’s settings/);
  assert.doesNotMatch(status.textContent, /VM → client/);
  assert.equal(status.hidden, false);
  assert.equal(status.dataset.blocked, 'true');
  const both = availability({to_vm: {...allowed, enabled: false}, from_vm: {...allowed, enabled: false}});
  assert.match(both.textContent, /VM → client clipboard is disabled/);
  assert.doesNotMatch(both.textContent, /clipboard is allowed/);
});

test('the popup distinguishes exhausted quotas, unavailable permissions and a missing guest agent', () => {
  const exhausted = availability({to_vm: {...allowed, limit: 10, remaining: 0}, from_vm: allowed});
  assert.match(exhausted.textContent, /Client → VM clipboard is blocked because its character allowance has been used up/);
  const unknown = availability(null);
  assert.match(unknown.textContent, /could not be verified.*Sharing is paused/);
  const offline = availability({to_vm: allowed, from_vm: allowed}, false);
  assert.match(offline.textContent, /SPICE guest agent with clipboard support/);
});

test('allowed sharing explains browser restrictions without repeating the character limit', () => {
  const full = availability({to_vm: allowed, from_vm: allowed});
  assert.equal(full.textContent, '');
  assert.equal(full.hidden, true);
  assert.equal(full.dataset.blocked, 'false');
  const browser = availability({to_vm: allowed, from_vm: allowed}, true, false);
  assert.match(browser.textContent, /Automatic clipboard sharing is unavailable in this browser.*manual copy and paste/);
  const limited = availability({to_vm: {...allowed, limit: 10, remaining: 5}, from_vm: allowed});
  assert.equal(limited.hidden, true);
});
