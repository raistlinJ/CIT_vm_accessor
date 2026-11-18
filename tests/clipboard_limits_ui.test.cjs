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
