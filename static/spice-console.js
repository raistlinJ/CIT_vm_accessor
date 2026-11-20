import { sendCtrlAltDel } from './vendor/spice-html5/src/main.js';
import { ClipboardSpiceConnection, clipboardExceedsAllowance } from './spice-clipboard.js?v=20261119-clipboard-silent-limit';
import { ClipboardSync, clipboardAccessHelp } from './spice-clipboard-sync.js?v=20261119-compact-clipboard';
import { Constants } from './vendor/spice-html5/src/enums.js';
import { handle_mousewheel } from './vendor/spice-html5/src/inputs.js';
import { WheelAccumulator } from './spice-scroll.js?v=20261119-clipboard-no-help';
import { ConsoleNetworkMetrics, formatNetworkBytes } from './spice-network.js?v=20261119-clipboard-no-help';

const loginDetails = document.getElementById('vm-login-details');
if (loginDetails) {
  const summary = loginDetails.querySelector('summary');
  const loginStatus = loginDetails.querySelector('[role="status"]');
  const loginFields = loginDetails.querySelector('dl');
  const values = [...loginFields.querySelectorAll('[data-credential]')];
  let loginRequest = null;

  function clearLoginDetails() {
    loginRequest?.abort();
    loginRequest = null;
    loginFields.hidden = true;
    loginStatus.hidden = true;
    for (const field of values) {
      field.textContent = '';
      field.parentElement.hidden = true;
    }
  }

  loginDetails.addEventListener('toggle', async () => {
    clearLoginDetails();
    if (!loginDetails.open) return;
    const controller = new AbortController();
    loginRequest = controller;
    loginStatus.textContent = 'Loading login details…';
    loginStatus.hidden = false;
    const loadError = 'Could not load VM login details. Close and reopen to try again.';
    try {
      const response = await fetch(loginDetails.dataset.url, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      });
      const data = await response.json();
      if (controller.signal.aborted || !loginDetails.open) return;
      if (!response.ok) {
        loginStatus.textContent = response.status === 401
          ? 'Sign in again to view VM login details.' : data.error || loadError;
        return;
      }
      for (const field of values) {
        const value = data.credentials?.[field.dataset.credential];
        if (typeof value !== 'string' || !value) continue;
        field.textContent = value;
        field.parentElement.hidden = false;
        loginFields.hidden = false;
      }
      loginStatus.textContent = loginFields.hidden ? 'No VM login details are configured.' : '';
      loginStatus.hidden = !loginFields.hidden;
    } catch {
      if (!controller.signal.aborted && loginDetails.open) loginStatus.textContent = loadError;
    }
  });

  loginDetails.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || !loginDetails.open) return;
    event.stopPropagation();
    loginDetails.open = false;
    summary.focus();
  });
  window.addEventListener('pagehide', () => {
    loginDetails.open = false;
    clearLoginDetails();
  });
}

const networkMetrics = new ConsoleNetworkMetrics();
const networkToggle = document.getElementById('network-enabled');
const networkReadout = document.getElementById('network-readout');
let networkTimer;
const networkHistory = [];
function renderNetworkMetrics() {
  const sample = networkMetrics.sample();
  networkHistory.push([sample.receivedRate, sample.sentRate]);
  if (networkHistory.length > 60) networkHistory.shift();
  const peak = Math.max(0, ...networkHistory.flat());
  const scale = Math.max(1, peak);
  for (const [index, direction] of ['received', 'sent'].entries()) {
    const points = networkHistory.map((rates, i) => {
      const x = (60 - networkHistory.length + i) * 240 / 59;
      const y = 63 - rates[index] / scale * 60;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    document.getElementById(`network-graph-${direction}`).setAttribute('points', points.join(' '));
  }
  document.getElementById('network-graph-scale').textContent = `${formatNetworkBytes(peak)}/s`;
  document.getElementById('network-received').textContent = `${formatNetworkBytes(sample.receivedRate)}/s`;
  document.getElementById('network-sent').textContent = `${formatNetworkBytes(sample.sentRate)}/s`;
  document.getElementById('network-total').textContent = `Received ${formatNetworkBytes(sample.received)} · Sent ${formatNetworkBytes(sample.sent)}`;
}
function updateNetworkMetrics() {
  clearInterval(networkTimer);
  networkMetrics.enable(networkToggle.checked);
  networkHistory.length = 0;
  networkReadout.hidden = !networkToggle.checked;
  if (networkToggle.checked) {
    renderNetworkMetrics();
    networkTimer = setInterval(renderNetworkMetrics, 1000);
  }
}
try { networkToggle.checked = localStorage.getItem('spice-network-enabled') === 'true'; } catch { }
networkToggle.addEventListener('change', () => {
  try { localStorage.setItem('spice-network-enabled', String(networkToggle.checked)); } catch { }
  updateNetworkMetrics();
});
updateNetworkMetrics();
window.addEventListener('pagehide', () => {
  clearInterval(networkTimer);
  networkMetrics.enable(false);
});
window.addEventListener('pageshow', event => { if (event.persisted) updateNetworkMetrics(); });

const status = document.getElementById('status');
const controlsDrawer = document.getElementById('controls-drawer');
const controlsToggle = document.getElementById('controls-toggle');
const controlsAlert = document.getElementById('controls-alert');
const controlsLoginCue = document.getElementById('controls-login-cue');
let hasOpenedControls = false;
let loginDetailsAvailable = false;
const displayNotice = document.getElementById('display-notice');
const consoleControls = document.getElementById('console-controls');
const screen = document.getElementById('spice-screen');
const scrollSpeed = document.getElementById('scroll-speed');
const wheel = new WheelAccumulator();
try {
  const saved = localStorage.getItem('spice-scroll-speed');
  if (['0.25', '0.5', '1', '2'].includes(saved)) scrollSpeed.value = saved;
} catch { /* Storage may be unavailable in private browsing. */ }
scrollSpeed.addEventListener('change', () => {
  wheel.reset();
  try { localStorage.setItem('spice-scroll-speed', scrollSpeed.value); } catch { }
});
screen.addEventListener('wheel', (event) => {
  const canvas = event.target;
  if (canvas.tagName !== 'CANVAS') return;
  event.preventDefault();
  event.stopImmediatePropagation();
  if (canvas.sc?.inputs?.state !== 'ready') { wheel.reset(); return; }
  const steps = wheel.consume(event, Number(scrollSpeed.value), screen.clientHeight);
  for (let i = 0; i < Math.abs(steps); i++) {
    handle_mousewheel.call(canvas, { deltaY: Math.sign(steps), preventDefault() {} });
  }
}, { capture: true, passive: false });
const area = document.getElementById('spice-area');
const viewport = document.getElementById('spice-viewport');
const screenshotButton = document.getElementById('take-screenshot');
const startVM = document.getElementById('start-vm');
const restartVM = document.getElementById('restart-vm');
const powerStatus = document.getElementById('power-status');
const powerCooldown = document.getElementById('power-cooldown');
const powerConfirm = document.getElementById('power-confirm');
const powerConfirmTitle = document.getElementById('power-confirm-title');
const powerConfirmMessage = document.getElementById('power-confirm-message');
const powerConfirmSubmit = document.getElementById('power-confirm-submit');
const reconnect = document.getElementById('reconnect');
const keys = document.getElementById('ctrl-alt-del');
const fallback = document.getElementById('fallback');
const clipboardStatus = document.getElementById('clipboard-status');
const clipboardPanel = document.getElementById('clipboard-panel');
// Keep access explanations visible even with a pre-update cached template.
const clipboardPolicyStatus = document.getElementById('clipboard-policy-status') ||
  clipboardPanel.insertBefore(document.createElement('p'), clipboardStatus);
clipboardPolicyStatus.id = 'clipboard-policy-status';
clipboardPolicyStatus.setAttribute('role', 'status');
clipboardPolicyStatus.setAttribute('aria-live', 'polite');
const clipboardToggle = document.getElementById('clipboard-toggle');
const clipboardPin = document.getElementById('clipboard-pin');
const clipboardClose = document.getElementById('clipboard-close');
const clipboardAutoTo = document.getElementById('clipboard-auto-to');
const clipboardAutoFrom = document.getElementById('clipboard-auto-from');
document.getElementById('clipboard-access-help')?.remove();
const clipboardAccess = clipboardAccessHelp({
  userAgent: navigator.userAgent, secure: window.isSecureContext,
  readable: Boolean(navigator.clipboard?.readText), writable: Boolean(navigator.clipboard?.writeText),
});
// A running server can still have an older template cached after its static
// files are updated. Optional clipboard UI must not prevent console startup.
const clipboardAutoControls = document.getElementById('clipboard-auto-controls');
if (clipboardAutoControls) clipboardAutoControls.hidden = !clipboardAccess.automaticSupported;
const filePanel = document.getElementById('file-panel');
const fileDisabled = document.getElementById('file-disabled');
const clipboardSend = document.getElementById('clipboard-send');
const clipboardReceive = document.getElementById('clipboard-receive');
const clipboardSendButton = document.getElementById('clipboard-send-button');
const clipboardCopy = document.getElementById('clipboard-copy');
const clipboardPaste = document.getElementById('clipboard-paste');
const clipboardReceiveButton = document.getElementById('clipboard-receive-button');
let clipboardRemaining = document.getElementById('clipboard-remaining');
// Older cached templates use a paragraph, which cannot contain a table.
if (clipboardRemaining?.tagName === 'P') {
  const container = document.createElement('div');
  container.id = clipboardRemaining.id;
  container.setAttribute('aria-live', 'polite');
  clipboardRemaining.replaceWith(container);
  clipboardRemaining = container;
}
let clipboardPolicy = null;
let clipboardUsageRequest = null;
let clipboardSendPending = false;

function clipboardLimited() {
  return !clipboardPolicy || Object.values(clipboardPolicy).some(rule => rule.limit !== null);
}

function renderClipboardAllowances() {
  if (!clipboardRemaining) return;
  let markup = '<p>Clipboard usage unavailable.</p>';
  if (clipboardPolicy) {
    const rows = Object.entries({to_vm: 'Client → VM', from_vm: 'VM → client'}).map(([direction, label]) => {
      const {enabled, remaining, used, limit} = clipboardPolicy[direction];
      let usage = '—';
      let balance = enabled ? 'Unlimited' : 'Disabled';
      if (enabled && limit !== null) {
        const percent = limit > 0 ? Number(remaining) / Number(limit) * 100 : 0;
        const band = percent > 75 ? 'high' : percent >= 30 ? 'medium' : 'low';
        const rounded = Math.round(percent * 10) / 10;
        let percentage = `${rounded.toLocaleString()}%`;
        if (band === 'high' && rounded === 75) percentage = '&gt;75%';
        if (band === 'low' && rounded === 30) percentage = '&lt;30%';
        usage = `${Number(used).toLocaleString()} / ${Number(limit).toLocaleString()}`;
        balance = `<span class="clipboard-balance" data-band="${band}">${Number(remaining).toLocaleString()} <small>${percentage}</small></span>`;
      }
      return `<tr><th scope="row">${label}</th><td>${usage}</td><td>${balance}</td></tr>`;
    }).join('');
    markup = `<table class="clipboard-allowances">
      <caption>Clipboard allowance (characters)</caption>
      <thead><tr><th scope="col">Direction</th><th scope="col">Used / limit</th><th scope="col">Remaining</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  }
  if (clipboardRemaining.innerHTML !== markup) clipboardRemaining.innerHTML = markup;
}

function updateClipboardAvailability() {
  if (!clipboardPolicy) {
    clipboardPolicyStatus.hidden = false;
    clipboardPolicyStatus.textContent = 'Clipboard access could not be verified. Sharing is paused until AccessForge can load this VM’s permissions.';
    clipboardPolicyStatus.dataset.blocked = 'true';
    return;
  }
  const rules = ['to_vm', 'from_vm'].map(direction => clipboardPolicy[direction]);
  const available = rules.some(rule => rule.enabled && rule.remaining !== 0);
  const restricted = rules.some(rule => !rule.enabled || rule.remaining === 0);
  const messages = rules.flatMap((rule, index) => {
    const label = index === 0 ? 'Client → VM' : 'VM → client';
    if (!rule.enabled) return [`${label} clipboard is disabled by this VM’s settings.`];
    if (rule.remaining === 0) return [`${label} clipboard is blocked because its character allowance has been used up.`];
    return [];
  });
  if (available && !connection?.clipboardReady) {
    messages.push('Sharing is unavailable until a SPICE guest agent with clipboard support is connected inside the VM.');
  } else if (available && !clipboardAccess.automaticSupported) {
    messages.push('Automatic clipboard sharing is unavailable in this browser. Use the text boxes for manual copy and paste.');
  }
  clipboardPolicyStatus.textContent = messages.join(' ');
  clipboardPolicyStatus.hidden = messages.length === 0;
  clipboardPolicyStatus.dataset.blocked = String(restricted || !connection?.clipboardReady);
}

function updateClipboardSendButton() {
  const rule = clipboardPolicy?.to_vm;
  clipboardSendButton.disabled = clipboardSendPending || !connection?.clipboardReady || !rule?.enabled || rule.remaining === 0 ||
    clipboardExceedsAllowance(clipboardSend.value, rule.remaining);
}

function applyClipboardPolicy() {
  const ready = Boolean(connection?.clipboardReady);
  const limited = clipboardLimited();
  const to = clipboardPolicy?.to_vm, from = clipboardPolicy?.from_vm;
  updateClipboardSendButton();
  if (clipboardReceiveButton) clipboardReceiveButton.disabled = !ready || !from?.enabled || from.remaining === 0 || !connection?.guestOwnsClipboard || connection?.awaitingClipboard;
  clipboardAutoTo.disabled = !ready || limited || !to?.enabled || !clipboardAccess.automaticSupported;
  clipboardAutoFrom.disabled = !ready || limited || !from?.enabled || !clipboardAccess.automaticSupported;
  if (clipboardAutoTo.disabled) { clipboardAutoTo.checked = false; clipboardSync.setToVM(false); }
  if (clipboardAutoFrom.disabled) { clipboardAutoFrom.checked = false; clipboardSync.setFromVM(false); }
  updateClipboardSyncTimer();
  if (connection) {
    connection.clipboardPolicy = clipboardPolicy || {to_vm: {enabled: false, remaining: 0}, from_vm: {enabled: false, remaining: 0}};
    if (!to?.enabled) connection.localClipboard = null;
  }
  if (!from?.enabled) { guestClipboard = null; clipboardReceive.value = ''; clipboardCopy.disabled = true; }
  renderClipboardAllowances();
  updateClipboardAvailability();
}

function refreshClipboardUsage() {
  // Polling and manual transfers need the same snapshot. Starting a newer
  // request must not turn a successful transfer preflight into a failure.
  if (clipboardUsageRequest) return clipboardUsageRequest.promise;
  const request = {controller: new AbortController()};
  clipboardUsageRequest = request;
  request.promise = fetchClipboardUsage(request).finally(() => {
    if (clipboardUsageRequest === request) clipboardUsageRequest = null;
  });
  return request.promise;
}

async function fetchClipboardUsage(request) {
  try {
    const response = await fetch(`/api/spice/${document.body.dataset.vmid}/clipboard`, {
      credentials: 'same-origin', cache: 'no-store', signal: request.controller.signal,
    });
    const data = await response.json();
    if (clipboardUsageRequest !== request) return false;
    if (!response.ok || !data.clipboard) throw new Error('Clipboard allowance unavailable');
    const previous = clipboardPolicy;
    clipboardPolicy = data.clipboard;
    for (const direction of ['to_vm', 'from_vm']) {
      if (clipboardPolicy[direction].denied_at && clipboardPolicy[direction].denied_at !== previous?.[direction]?.denied_at) {
        if (direction === 'to_vm') {
          if (previous) showClipboardStatus('');
          continue;
        }
        if (connection) {
          clearTimeout(clipboardReceiveTimer);
          connection.awaitingClipboard = false;
        }
        showClipboardStatus('A VM → client transfer exceeded the remaining allowance. Use a smaller selection. No characters were charged.', true);
      }
    }
    applyClipboardPolicy();
    return true;
  } catch {
    if (clipboardUsageRequest !== request) return false;
    clipboardPolicy = null;
    applyClipboardPolicy();
    return false;
  }
}
const MAX_VM_FILE_BYTES = 1024 * 1024 * 1024;
const fileUpload = document.getElementById('file-upload');
const fileUploadStatus = document.getElementById('file-upload-status');
const fileDownloadPath = document.getElementById('file-download-path');
const fileDownload = document.getElementById('file-download');
const fileDownloadStatus = document.getElementById('file-download-status');
let connection = null;
let uploadAllowed = false;
let downloadAllowed = false;
let pending = null;
let generation = 0;
let resizeTimer;
let connectTimer;
let guestClipboard = null;
let lastGuestSize = null;
let takingScreenshot = false;
let powerRequest = null;
let connectionIntent = 0;
const backlogRecoveries = [];
let powerCooldownUntil = 0;
let powerCooldownTimer;
let pendingPowerAction = null;
let fileDownloadController = null;
let clipboardSyncTimer;
let clipboardReceiveTimer;

const clipboardSync = new ClipboardSync({
  readText: () => {
    if (!navigator.clipboard?.readText) throw new Error('Browser clipboard access is unavailable.');
    return navigator.clipboard.readText();
  },
  writeText: text => {
    if (!navigator.clipboard?.writeText) throw new Error('Browser clipboard access is unavailable.');
    return navigator.clipboard.writeText(text);
  },
  offerText: text => connection.offerClipboard(text),
  isActive: () => Boolean(!clipboardLimited() && connection?.clipboardReady && document.hasFocus() && !document.hidden),
  onSend(text) {
    clipboardSend.value = text;
    showClipboardStatus('Copied to the VM clipboard. Paste inside the VM.');
  },
  onCopy() { showClipboardStatus('Copied to your clipboard.'); },
  onError(direction, error) {
    (direction === 'to' ? clipboardAutoTo : clipboardAutoFrom).checked = false;
    updateClipboardSyncTimer();
    const action = direction === 'to' ? 'Auto-copy to VM clipboard' : 'Auto-copy to my clipboard';
    const denied = error.name === 'NotAllowedError';
    const reason = denied ? 'Browser clipboard access was denied.' : error.message;
    showClipboardStatus(`${action} paused. ${reason} Use the manual clipboard controls or enable it again to retry.`, true);
  },
});

function updateClipboardSyncTimer() {
  clearInterval(clipboardSyncTimer);
  if (clipboardAutoTo.checked) clipboardSyncTimer = setInterval(() => clipboardSync.poll(), 750);
}

function setClipboardOpen(open) {
  clipboardPanel.hidden = !open;
  clipboardToggle.hidden = open;
  clipboardToggle.setAttribute('aria-expanded', String(open));
  resize();
}

function setClipboardPinned(pinned) {
  clipboardPanel.dataset.pinned = String(pinned);
  clipboardPin.setAttribute('aria-pressed', String(pinned));
  clipboardPin.textContent = pinned ? 'Unpin' : 'Pin';
  clipboardPin.title = pinned ? 'Unpin clipboard' : 'Pin clipboard and fit the VM beside it';
  try { localStorage.setItem('spice-clipboard-pinned', String(pinned)); } catch { }
  resize();
}

clipboardToggle.addEventListener('click', () => setClipboardOpen(true));
document.getElementById('open-clipboard').addEventListener('click', () => {
  setControlsOpen(false);
  setClipboardOpen(true);
  clipboardPin.focus();
});
clipboardPin.addEventListener('click', () => setClipboardPinned(clipboardPanel.dataset.pinned !== 'true'));
clipboardClose.addEventListener('click', () => {
  setClipboardOpen(false);
  clipboardToggle.focus();
});
clipboardPanel.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    setClipboardOpen(false);
    clipboardToggle.focus();
  }
});
clipboardAutoTo.addEventListener('change', () => {
  if (clipboardAutoTo.disabled) { clipboardAutoTo.checked = false; return; }
  clipboardSync.setToVM(clipboardAutoTo.checked);
  updateClipboardSyncTimer();
});
clipboardAutoFrom.addEventListener('change', () => {
  if (clipboardAutoFrom.disabled) { clipboardAutoFrom.checked = false; return; }
  clipboardSync.setFromVM(clipboardAutoFrom.checked);
});
clipboardReceiveButton?.addEventListener('click', async () => {
  const attempt = generation;
  clipboardReceiveButton.disabled = true;
  try {
    const verified = await refreshClipboardUsage();
    if (attempt !== generation) return;
    if (!verified) throw new Error('Clipboard allowance is unavailable. Try again.');
    const rule = clipboardPolicy.from_vm;
    if (!rule.enabled || rule.remaining === 0) return;
    if (!connection?.requestClipboard()) throw new Error('Copy plain text inside the VM, then click Get VM text.');
    showClipboardStatus('Requesting text from the VM…');
  } catch (error) {
    if (attempt === generation) showClipboardStatus(error.message, true);
  } finally {
    if (attempt === generation) applyClipboardPolicy();
  }
});
setInterval(() => { if (connection && !document.hidden) void refreshClipboardUsage(); }, 2000);
function resumeClipboardSync() {
  // Flush a guest copy first so a stale computer clipboard cannot replace it.
  void clipboardSync.copyGuest();
  void clipboardSync.poll();
}
window.addEventListener('focus', resumeClipboardSync);
document.addEventListener('visibilitychange', resumeClipboardSync);
screen.addEventListener('pointerdown', resumeClipboardSync, { capture: true });
try {
  if (localStorage.getItem('spice-clipboard-pinned') === 'true') {
    setClipboardPinned(true);
    setClipboardOpen(true);
  }
} catch { }

function showFileStatus(element, message, error = false) {
  if (!element) return;
  element.textContent = message;
  element.dataset.error = String(error);
  updateDrawerIndicator();
}

function updateFileTransferAvailability() {
  const available = uploadAllowed || downloadAllowed;
  if (filePanel) {
    filePanel.hidden = !available;
    if (!available) filePanel.open = false;
  }
  fileDisabled.hidden = available;
}

if (fileUpload) {
  fileUpload.addEventListener('change', () => {
    const files = Array.from(fileUpload.files || []);
    fileUpload.value = '';
    if (!files.length) return;
    if (!uploadAllowed || !connection?.agent_connected) {
      showFileStatus(fileUploadStatus, 'Sending requires a connected SPICE guest agent.', true);
      return;
    }
    for (const file of files) {
      if (file.size > MAX_VM_FILE_BYTES) {
        showFileStatus(fileUploadStatus, `${file.name} exceeds the 1 GiB limit.`, true);
        continue;
      }
      connection.file_xfer_start(file);
      showFileStatus(fileUploadStatus, `Sending ${file.name} to the VM…`);
    }
  });
}

if (fileDownload) {
  fileDownload.addEventListener('click', async () => {
    if (fileDownloadController || !downloadAllowed) return;
    const path = fileDownloadPath.value;
    if (!path.trim()) {
      showFileStatus(fileDownloadStatus, 'Enter the full path to a file in the VM.', true);
      return;
    }
    const controller = new AbortController();
    fileDownloadController = controller;
    fileDownload.disabled = true;
    showFileStatus(fileDownloadStatus, 'Downloading from VM…');
    try {
      const chunks = [];
      let offset = 0;
      while (true) {
        if (offset > MAX_VM_FILE_BYTES) throw new Error('Files over 1 GiB cannot be downloaded.');
        const response = await fetch(`/api/spice/${document.body.dataset.vmid}/file-read`, {
          method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
          headers: { 'Content-Type': 'application/json', 'X-Console-CSRF': document.body.dataset.csrf },
          body: JSON.stringify({ path, offset }),
        });
        if (!response.ok) {
          const message = response.headers.get('Content-Type')?.includes('application/json')
            ? (await response.json()).error : null;
          throw new Error(message || 'Could not download the VM file.');
        }
        if (response.headers.get('Content-Type')?.split(';')[0] !== 'application/octet-stream') {
          throw new Error('The file service returned an invalid response.');
        }
        const bytes = await response.arrayBuffer();
        const chunkLimit = response.headers.get('X-File-Legacy') === 'true' ? 16 : 1;
        if (bytes.byteLength > chunkLimit * 1024 * 1024 || (!bytes.byteLength && response.headers.get('X-File-More') === 'true')) {
          throw new Error('The VM returned an invalid file chunk.');
        }
        if (offset + bytes.byteLength > MAX_VM_FILE_BYTES) {
          throw new Error('Files over 1 GiB cannot be downloaded.');
        }
        chunks.push(bytes);
        offset += bytes.byteLength;
        showFileStatus(fileDownloadStatus, `Downloaded ${Math.round(offset / 1024)} KiB from VM…`);
        if (response.headers.get('X-File-More') !== 'true') break;
      }
      const filename = (path.split(/[\\/]/).pop() || 'vm-file').replace(/[\x00-\x1f<>:"|?*]/g, '_').slice(0, 128);
      const url = URL.createObjectURL(new Blob(chunks, { type: 'application/octet-stream' }));
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      try { link.click(); } finally {
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
      }
      showFileStatus(fileDownloadStatus, `Downloaded ${filename} from VM.`);
    } catch (error) {
      if (controller.signal.reason !== 'pagehide') showFileStatus(fileDownloadStatus,
        error.message || 'Could not download the VM file.', true);
    } finally {
      fileDownloadController = null;
      fileDownload.disabled = false;
    }
  });
}

function updatePowerButtons() {
  clearTimeout(powerCooldownTimer);
  const remaining = Math.max(0, Math.ceil((powerCooldownUntil - performance.now()) / 1000));
  startVM.disabled = restartVM.disabled = Boolean(powerRequest) || powerConfirm.open || remaining > 0;
  powerCooldown.hidden = remaining === 0;
  powerCooldown.textContent = remaining ? `Please wait ${remaining}s before another power request.` : '';
  if (remaining) powerCooldownTimer = setTimeout(updatePowerButtons, 250);
}

async function powerVM(action) {
  if (powerRequest || performance.now() < powerCooldownUntil) return;
  const controller = new AbortController();
  powerRequest = controller;
  const intent = connectionIntent;
  powerCooldownUntil = performance.now() + 10000;
  updatePowerButtons();
  powerStatus.hidden = false;
  powerStatus.dataset.error = 'false';
  updateDrawerIndicator();
  powerStatus.textContent = action === 'start' ? 'Starting VM…' : 'Restarting VM…';
  const timeout = setTimeout(() => controller.abort('timeout'), 180000);
  try {
    async function requestJSON(url, options = {}) {
      const response = await fetch(url, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal, ...options,
      });
      if (!response.headers.get('Content-Type')?.includes('application/json')) {
        throw new Error('The console service is unavailable. Reload this page or sign in again.');
      }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'The VM power request failed.');
      return data;
    }
    const result = await requestJSON(document.body.dataset.powerUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Console-CSRF': document.body.dataset.csrf },
      body: JSON.stringify({ action }),
    });
    if (!result.done) {
      if (!result.task_url) throw new Error('No task was returned. Check the VM state in Proxmox.');
      while (true) {
        const task = await requestJSON(result.task_url);
        if (task.done) break;
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
    }
    powerStatus.textContent = result.message || (action === 'start' ? 'VM started.' : 'VM restarted.');
    // A manual reconnect during the task overrides auto-reconnect.
    if (intent === connectionIntent && !controller.signal.aborted) {
      const waitForRunningUntil = action === 'start' ? performance.now() + 30000 : 0;
      connect(waitForRunningUntil);
    }
  } catch (error) {
    if (controller.signal.reason === 'pagehide') return;
    powerStatus.dataset.error = 'true';
    powerStatus.textContent = controller.signal.aborted
      ? 'The power task is taking longer than expected. Check its status in Proxmox before trying again.'
      : error.message || 'Could not confirm the power request. Check the VM state in Proxmox before trying again.';
    updateDrawerIndicator();
  } finally {
    clearTimeout(timeout);
    powerRequest = null;
    updatePowerButtons();
  }
}

function confirmPowerVM(action) {
  if (powerRequest || powerConfirm.open || performance.now() < powerCooldownUntil) return;
  pendingPowerAction = action;
  powerConfirm.returnValue = '';
  const verb = action === 'start' ? 'Start' : 'Restart';
  powerConfirmTitle.textContent = `${verb} VM?`;
  powerConfirmMessage.textContent = `${verb}ing the VM can take at least 60 seconds. Continue?`;
  powerConfirmSubmit.textContent = `${verb} VM`;
  powerConfirm.showModal();
  updatePowerButtons();
}

powerConfirm.addEventListener('close', () => {
  const action = pendingPowerAction;
  pendingPowerAction = null;
  if (powerConfirm.returnValue === 'confirm' && action) powerVM(action);
  updatePowerButtons();
});

startVM.addEventListener('click', () => confirmPowerVM('start'));
restartVM.addEventListener('click', () => confirmPowerVM('restart'));

function setControlsOpen(open) {
  if (open) hasOpenedControls = true;
  if (loginDetails && (!open || consoleControls.contains(loginDetails))) loginDetails.open = open;
  consoleControls.hidden = !open;
  controlsDrawer.dataset.open = String(open);
  controlsToggle.setAttribute('aria-expanded', String(open));
  updateDrawerIndicator();
}

function updateDrawerIndicator() {
  const clipboardError = clipboardStatus.dataset.error === 'true';
  const fileError = [fileUploadStatus, fileDownloadStatus].some(element => element?.dataset.error === 'true');
  clipboardPanel.dataset.error = String(clipboardError);
  clipboardToggle.dataset.error = String(clipboardError);
  clipboardToggle.setAttribute('aria-label', `Open clipboard${clipboardError ? ' — attention needed' : ''}`);
  clipboardToggle.title = clipboardToggle.getAttribute('aria-label');
  if (filePanel) {
    filePanel.dataset.error = String(fileError);
    filePanel.querySelector('summary').setAttribute('aria-label',
      `File Transfer${fileError ? ' — attention needed' : ''}`);
  }
  const error = [status, powerStatus, clipboardStatus, fileUploadStatus, fileDownloadStatus]
    .some(element => element?.dataset.error === 'true');
  controlsToggle.dataset.error = String(error);
  controlsAlert.hidden = !error;
  const loginCue = loginDetailsAvailable && !hasOpenedControls;
  if (controlsLoginCue) controlsLoginCue.hidden = !loginCue;
  controlsToggle.dataset.loginCue = String(loginCue);
  const action = consoleControls.hidden ? 'Open' : 'Close';
  const label = `${action} console controls${error ? ' — attention needed' : ''}${loginCue ? ' — VM login details available' : ''}`;
  controlsToggle.setAttribute('aria-label', label);
  controlsToggle.title = label;
}

async function checkLoginAvailability() {
  const url = loginDetails?.dataset.availabilityUrl;
  if (!url) return;
  try {
    const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) return;
    const { available } = await response.json();
    if (available === true && !hasOpenedControls) {
      loginDetailsAvailable = true;
      updateDrawerIndicator();
    }
  } catch { /* The drawer can still load login details when opened. */ }
}

controlsToggle.addEventListener('click', () => setControlsOpen(consoleControls.hidden));
controlsDrawer.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !consoleControls.hidden) {
    event.preventDefault();
    event.stopPropagation();
    setControlsOpen(false);
    controlsToggle.focus();
  }
});

function showClipboardStatus(message, error = false) {
  clipboardStatus.hidden = !message;
  clipboardStatus.textContent = message;
  clipboardStatus.dataset.error = String(error);
  updateDrawerIndicator();
}

function showStatus(message, error = false, code = '') {
  status.textContent = message;
  status.dataset.error = String(error);
  displayNotice.hidden = code !== 'vm_stopped';
  displayNotice.textContent = code === 'vm_stopped' ? message : '';
  updateDrawerIndicator();
}

function stop() {
  generation++;
  clipboardSendPending = false;
  clipboardUsageRequest?.controller.abort();
  clipboardUsageRequest = null;
  clearTimeout(clipboardReceiveTimer);
  clipboardPolicy = null;
  renderClipboardAllowances();
  if (clipboardReceiveButton) clipboardReceiveButton.disabled = true;
  clipboardSync.reset();
  clipboardAutoTo.checked = clipboardAutoFrom.checked = false;
  clipboardAutoTo.disabled = clipboardAutoFrom.disabled = true;
  updateClipboardSyncTimer();
  pending?.abort();
  pending = null;
  clearTimeout(resizeTimer);
  clearTimeout(connectTimer);
  const old = connection;
  connection = null;
  old?.stop();
  networkMetrics.reset();
  networkHistory.length = 0;
  if (networkToggle.checked) renderNetworkMetrics();
  screen.replaceChildren();
  lastGuestSize = null;
  fitDisplay();
  document.getElementById('message-div').replaceChildren();
  reconnect.disabled = false;
  keys.disabled = true;
  guestClipboard = null;
  clipboardSend.value = '';
  clipboardReceive.value = '';
  clipboardSendButton.disabled = true;
  clipboardCopy.disabled = true;
  clipboardPaste.disabled = false;
  if (fileUpload) {
    fileUpload.disabled = true;
    fileUpload.value = '';
    document.getElementById('spice-xfer-area').replaceChildren();
    showFileStatus(fileUploadStatus, 'Sending requires a connected SPICE guest agent.');
  }
  showClipboardStatus('Clipboard sharing requires a connected SPICE guest agent.');
  clipboardPolicyStatus.textContent = 'Clipboard sharing is unavailable while the VM console is disconnected.';
  clipboardPolicyStatus.hidden = false;
  clipboardPolicyStatus.dataset.blocked = 'true';
}

function fitDisplay() {
  const canvas = screen.querySelector('canvas');
  screenshotButton.disabled = takingScreenshot || !canvas?.width || !canvas?.height;
  if (!canvas) {
    viewport.style.width = viewport.style.height = '0px';
    screen.style.transform = '';
    screen.style.left = screen.style.top = '0px';
    return;
  }
  if (!canvas.width || !canvas.height || !area.clientWidth || !area.clientHeight) return;
  const monitor = canvas.spiceMonitor;
  const width = monitor?.width || canvas.width;
  const height = monitor?.height || canvas.height;
  const scale = Math.min(1, area.clientWidth / width, area.clientHeight / height);
  // Transform the entire surface (including video/cursor overlays). The
  // canvas keeps its native dimensions; input handlers map pointer locations
  // back into that coordinate space so SPICE mouse positions remain accurate.
  screen.style.width = `${canvas.width}px`;
  screen.style.height = `${canvas.height}px`;
  screen.style.transform = `scale(${scale})`;
  screen.style.left = `${-(monitor?.x || 0) * scale}px`;
  screen.style.top = `${-(monitor?.y || 0) * scale}px`;
  viewport.style.width = `${width * scale}px`;
  viewport.style.height = `${height * scale}px`;
}

function requestGuestResize() {
  if (!connection?.agent_connected || !area.clientWidth || !area.clientHeight) return;
  const width = Math.max(320, Math.floor(area.clientWidth / 8) * 8);
  const height = Math.max(200, Math.floor(area.clientHeight / 8) * 8);
  const size = `${width}x${height}`;
  if (size === lastGuestSize) return;
  connection.resize_window(0, width, height, 32, 0, 0);
  lastGuestSize = size;
}

function resize() {
  fitDisplay();
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(requestGuestResize, 200);
}

async function connect(waitForRunningUntil = 0) {
  stop();
  const attempt = generation;
  reconnect.disabled = true;
  showStatus('Connecting…');
  pending = new AbortController();
  try {
    const response = await fetch(document.body.dataset.sessionUrl, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'X-Console-CSRF': document.body.dataset.csrf },
      signal: pending.signal,
    });
    if (!response.headers.get('Content-Type')?.includes('application/json')) {
      throw new Error('The console service is unavailable. Reload this page or sign in again.');
    }
    const config = await response.json();
    uploadAllowed = config.file_upload === true;
    downloadAllowed = config.file_download === true;
    if (fileDownload) fileDownload.disabled = !downloadAllowed;
    if (fileUploadStatus && !uploadAllowed) showFileStatus(fileUploadStatus, 'Uploads are disabled for this VM.');
    if (fileDownloadStatus && !downloadAllowed) showFileStatus(fileDownloadStatus, 'Downloads are disabled for this VM.');
    if (attempt !== generation) return;
    pending = null;
    if (config.fallback) {
      fallback.href = config.fallback;
      fallback.hidden = false;
    }
    if (!response.ok) {
      const error = new Error(config.error || 'Could not open the console.');
      error.code = config.code;
      throw error;
    }
    updateFileTransferAvailability();
    await refreshClipboardUsage();
    if (attempt !== generation) return;
    document.getElementById('console-title').textContent = config.title;
    document.title = `${config.title} · AccessForge`;
    const url = new URL(config.websocket, window.location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    connection = new ClipboardSpiceConnection({
      clipboardPolicy: clipboardPolicy || {to_vm: {enabled:false, remaining:0}, from_vm: {enabled:false, limit:0, remaining:0}},
      networkObserver: networkMetrics.observer,
      uri: url.href, password: config.password,
      screen_id: 'spice-screen', message_id: 'message-div', dump_id: 'debug-div',
      onsuccess() {
        if (attempt !== generation) return;
        showStatus('Connected. Click inside the display to use the VM.');
        reconnect.disabled = false;
        keys.disabled = false;
      },
      onagent() {
        if (attempt !== generation) return;
        if (fileUpload) fileUpload.disabled = !uploadAllowed;
        lastGuestSize = null;
        resize();
      },
      onmonitorchange() {
        if (attempt === generation) fitDisplay();
      },
      onclipboardstate(ready) {
        if (attempt !== generation) return;
        applyClipboardPolicy();
        if (!ready) {
          clipboardSync.reset();
          clipboardAutoTo.checked = clipboardAutoFrom.checked = false;
          updateClipboardSyncTimer();
        }
        if (fileUpload) fileUpload.disabled = !uploadAllowed || !connection?.agent_connected;
        showClipboardStatus(ready ? '' :
          'Clipboard sharing requires a connected SPICE guest agent.');
      },
      onclipboard(text) {
        if (attempt !== generation) return;
        clearTimeout(clipboardReceiveTimer);
        guestClipboard = text;
        clipboardReceive.value = text ?? '';
        clipboardCopy.disabled = text === null;
        if (text !== null) showClipboardStatus('VM text is ready to copy.');
        void clipboardSync.receiveGuest(text);
        applyClipboardPolicy();
        if (text !== null) void refreshClipboardUsage();
      },
      onclipboardavailable() { applyClipboardPolicy(); },
      onclipboardempty() {
        if (attempt !== generation) return;
        clearTimeout(clipboardReceiveTimer);
        applyClipboardPolicy();
        showClipboardStatus('The VM returned no text. Copy plain text inside the VM and try again.', true);
      },
      onclipboarderror(message) {
        if (attempt === generation) showClipboardStatus(message, true);
      },
      onclipboardrequested() {
        if (attempt !== generation) return;
        clearTimeout(clipboardReceiveTimer);
        clipboardReceiveTimer = setTimeout(() => {
          if (attempt !== generation || !connection?.awaitingClipboard) return;
          connection.awaitingClipboard = false;
          applyClipboardPolicy();
          showClipboardStatus('No clipboard text received. Copy a smaller plain-text selection inside the VM and retry.', true);
        }, 15000);
      },
      onfiletransfer(name, error) {
        if (attempt !== generation) return;
        showFileStatus(fileUploadStatus, error ? `Could not send ${name}: ${error}` : `Sent ${name} to the VM.`, Boolean(error));
      },
      onerror() {
        if (attempt !== generation) return;
        stop();
        showStatus('Connection lost or unavailable. Click Re-Fit Window, or try noVNC.', true);
      },
      onbacklog() {
        if (attempt !== generation) return;
        const now = Date.now();
        while (backlogRecoveries.length && now - backlogRecoveries[0] > 60000)
          backlogRecoveries.shift();
        if (backlogRecoveries.length >= 2) {
          stop();
          showStatus('The SPICE display is repeatedly falling behind. Try noVNC.', true);
          return;
        }
        backlogRecoveries.push(now);
        showStatus('The SPICE display fell behind. Reconnecting…');
        setTimeout(() => { if (attempt === generation) connect(); }, 0);
      },
    });
    connectTimer = setTimeout(() => {
      if (attempt === generation && !screen.querySelector('canvas')) {
        stop();
        showStatus('The VM display did not become ready. Click Re-Fit Window, or try noVNC.', true);
      }
    }, 20000);
  } catch (error) {
    if (attempt !== generation || error.name === 'AbortError') return;
    // Proxmox's cluster status can still report stopped after a successful start.
    if (error.code === 'vm_stopped' && performance.now() < waitForRunningUntil) {
      showStatus('Waiting for the VM to come online…');
      reconnect.disabled = false;
      connectTimer = setTimeout(() => {
        if (attempt === generation) connect(waitForRunningUntil);
      }, 2000);
      return;
    }
    stop();
    showStatus(error.message || 'Could not open the console.', true, error.code);
  }
}

reconnect.addEventListener('click', () => { connectionIntent++; backlogRecoveries.length = 0; area.scrollTo(0, 0); connect(); });
fallback.addEventListener('click', stop);
screenshotButton.addEventListener('click', async () => {
  const canvas = screen.querySelector('canvas');
  if (takingScreenshot || !canvas?.width || !canvas?.height) return;
  const attempt = generation;
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  takingScreenshot = true;
  screenshotButton.disabled = true;
  try {
    // Capture the visible monitor at native resolution, independently of CSS scale.
    const monitor = canvas.spiceMonitor;
    const originX = monitor?.x || 0;
    const originY = monitor?.y || 0;
    const snapshot = document.createElement('canvas');
    snapshot.width = monitor?.width || canvas.width;
    snapshot.height = monitor?.height || canvas.height;
    const context = snapshot.getContext('2d');
    context.drawImage(canvas, -originX, -originY);
    // VP8 streams are separate video overlays; include their current frames
    // in DOM paint order, preserving the guest's clip and vertical direction.
    for (const video of screen.querySelectorAll('video')) {
      const stream = video.spice_stream;
      if (!stream || video.readyState < 2) continue;
      context.save();
      context.translate(-originX, -originY);
      if (stream.clip?.type === Constants.SPICE_CLIP_TYPE_RECTS) {
        context.beginPath();
        for (const rect of stream.clip.rects?.rects || []) {
          context.rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top);
        }
        context.clip();
      }
      context.translate(video.offsetLeft - canvas.offsetLeft, video.offsetTop - canvas.offsetTop);
      if (!(stream.flags & Constants.SPICE_STREAM_FLAGS_TOP_DOWN)) {
        context.translate(0, video.offsetHeight);
        context.scale(1, -1);
      }
      context.drawImage(video, 0, 0, video.offsetWidth, video.offsetHeight);
      context.restore();
    }
    const blob = await new Promise(resolve => snapshot.toBlob(resolve, 'image/png'));
    if (attempt !== generation) return;
    if (!blob) throw new Error('PNG encoding failed');
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `VM-${document.body.dataset.vmid}-${timestamp}.png`;
    document.body.appendChild(link);
    try {
      link.click();
    } finally {
      link.remove();
      // Keep the URL alive long enough for browsers to start the download.
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
    showStatus('Screenshot download started.');
  } catch {
    if (attempt === generation) showStatus('Could not capture the VM display. Try taking the screenshot again.', true);
  } finally {
    takingScreenshot = false;
    fitDisplay();
  }
});
keys.addEventListener('click', () => {
  if (connection?.inputs?.state === 'ready') sendCtrlAltDel(connection);
  screen.querySelector('canvas')?.focus();
});
clipboardSend.addEventListener('input', updateClipboardSendButton);
clipboardPaste.addEventListener('click', async () => {
  const attempt = generation;
  const draft = clipboardSend.value;
  clipboardPaste.disabled = true;
  try {
    if (!navigator.clipboard?.readText) throw new Error('Clipboard API unavailable');
    const text = await navigator.clipboard.readText();
    if (attempt !== generation) return;
    if (clipboardSend.value !== draft) {
      showClipboardStatus('The text box changed while clipboard access was pending. Click Paste here again to replace it.');
      return;
    }
    clipboardSend.value = text;
    updateClipboardSendButton();
    showClipboardStatus('');
  } catch {
    if (attempt !== generation) return;
    clipboardSend.focus();
    showClipboardStatus('Browser clipboard access is blocked. Paste into the text box with Ctrl+V or ⌘V, then click Copy to VM.', true);
  } finally {
    if (attempt === generation) clipboardPaste.disabled = false;
  }
});
clipboardSendButton.addEventListener('click', async () => {
  if (clipboardSendPending) return;
  const attempt = generation;
  clipboardSendPending = true;
  try {
    clipboardSendButton.disabled = true;
    const verified = await refreshClipboardUsage();
    if (attempt !== generation) return;
    if (!verified) throw new Error('Clipboard allowance is unavailable. Try again.');
    if (!connection) throw new Error('Connect to the VM first.');
    if (!connection.offerClipboard(clipboardSend.value)) {
      showClipboardStatus('');
      return;
    }
    clipboardSync.rememberHost(clipboardSend.value);
    showClipboardStatus('Click inside the VM and paste the text.');
  } catch (error) {
    if (attempt === generation) showClipboardStatus(error.message, true);
  } finally {
    if (attempt === generation) {
      clipboardSendPending = false;
      applyClipboardPolicy();
    }
  }
});
clipboardCopy.addEventListener('click', async () => {
  if (guestClipboard === null) return;
  const attempt = generation;
  const text = guestClipboard;
  try {
    // Invoke within this click, without first awaiting a network request. This
    // preserves transient user activation required by Firefox and Safari.
    if (!navigator.clipboard?.writeText) throw new Error('Clipboard API unavailable');
    await navigator.clipboard.writeText(text);
    if (attempt === generation) clipboardSync.rememberHost(text);
    if (attempt === generation && guestClipboard === text) showClipboardStatus('Copied VM text to your computer.');
  } catch {
    if (attempt !== generation || guestClipboard !== text) return;
    clipboardReceive.focus();
    clipboardReceive.select();
    showClipboardStatus('Browser clipboard access is blocked. The VM text is selected; copy it with Ctrl+C or ⌘C.', true);
  }
});
new ResizeObserver(resize).observe(area);
new MutationObserver(fitDisplay).observe(screen, {
  childList: true, subtree: true, attributes: true, attributeFilter: ['width', 'height'],
});
document.addEventListener('fullscreenchange', resize);
window.addEventListener('resize', resize);
window.addEventListener('pagehide', () => { powerRequest?.abort('pagehide'); fileDownloadController?.abort('pagehide'); stop(); });
window.addEventListener('pageshow', event => { if (event.persisted) connect(); });
void checkLoginAvailability();
connect();
