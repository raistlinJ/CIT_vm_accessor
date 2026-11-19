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
    clipboardSendButton:box(),clipboardReceiveButton:box(),clipboardAutoTo:box(),clipboardAutoFrom:box(),
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
  assert.match(context.clipboardRemaining.textContent,/12 copy\/paste characters remaining/);
  assert.match(context.clipboardRemaining.textContent,/VM → client: unlimited/);
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

test('the popup clearly names disabled directions without saying they are ready', () => {
  const status = availability({to_vm: {...allowed, enabled: false}, from_vm: allowed});
  assert.match(status.textContent, /Client → VM clipboard is disabled by this VM’s settings/);
  assert.match(status.textContent, /VM → client clipboard is allowed/);
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

test('allowed sharing explains browser restrictions and manual-only character limits', () => {
  const full = availability({to_vm: allowed, from_vm: allowed});
  assert.equal(full.textContent, 'Clipboard is allowed in both directions.');
  assert.equal(full.dataset.blocked, 'false');
  const browser = availability({to_vm: allowed, from_vm: allowed}, true, false);
  assert.match(browser.textContent, /Automatic clipboard sharing is unavailable in this browser.*manual copy and paste/);
  const limited = availability({to_vm: {...allowed, limit: 10, remaining: 5}, from_vm: allowed});
  assert.match(limited.textContent, /Automatic sharing is disabled while a character limit is configured/);
});
