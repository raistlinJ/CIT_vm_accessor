import { sendCtrlAltDel } from './vendor/spice-html5/src/main.js';
import { ClipboardSpiceConnection } from './spice-clipboard.js';

const status = document.getElementById('status');
const controlsDrawer = document.getElementById('controls-drawer');
const controlsToggle = document.getElementById('controls-toggle');
const consoleControls = document.getElementById('console-controls');
const screen = document.getElementById('spice-screen');
const area = document.getElementById('spice-area');
const viewport = document.getElementById('spice-viewport');
const fitWindow = document.getElementById('fit-window');
const reconnect = document.getElementById('reconnect');
const disconnect = document.getElementById('disconnect');
const keys = document.getElementById('ctrl-alt-del');
const fallback = document.getElementById('fallback');
const clipboardToggle = document.getElementById('clipboard-toggle');
const clipboardPanel = document.getElementById('clipboard-panel');
const clipboardStatus = document.getElementById('clipboard-status');
const clipboardSend = document.getElementById('clipboard-send');
const clipboardReceive = document.getElementById('clipboard-receive');
const clipboardSendButton = document.getElementById('clipboard-send-button');
const clipboardCopy = document.getElementById('clipboard-copy');
const clipboardPaste = document.getElementById('clipboard-paste');
let connection = null;
let pending = null;
let generation = 0;
let resizeTimer;
let connectTimer;
let guestClipboard = null;
let lastGuestSize = null;

function setControlsOpen(open) {
  consoleControls.hidden = !open;
  controlsDrawer.dataset.open = String(open);
  controlsToggle.setAttribute('aria-expanded', String(open));
  controlsToggle.setAttribute('aria-label', open ? 'Close console controls' : 'Open console controls');
  controlsToggle.title = open ? 'Close console controls' : 'Open console controls';
  controlsToggle.firstElementChild.textContent = open ? '‹' : '☰';
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
  clipboardStatus.textContent = message;
  clipboardStatus.dataset.error = String(error);
}

function showStatus(message, error = false) {
  status.textContent = message;
  status.dataset.error = String(error);
  if (error) setControlsOpen(true);
}

function stop() {
  generation++;
  pending?.abort();
  pending = null;
  clearTimeout(resizeTimer);
  clearTimeout(connectTimer);
  const old = connection;
  connection = null;
  old?.stop();
  screen.replaceChildren();
  lastGuestSize = null;
  fitDisplay();
  document.getElementById('message-div').replaceChildren();
  reconnect.disabled = false;
  disconnect.disabled = true;
  keys.disabled = true;
  guestClipboard = null;
  clipboardSend.value = '';
  clipboardReceive.value = '';
  clipboardSendButton.disabled = true;
  clipboardCopy.disabled = true;
  clipboardPaste.disabled = false;
  showClipboardStatus('Clipboard sharing requires a connected SPICE guest agent.');
}

function fitDisplay() {
  const canvas = screen.querySelector('canvas');
  fitWindow.disabled = !canvas;
  if (!canvas) {
    viewport.style.width = viewport.style.height = '0px';
    screen.style.transform = '';
    return;
  }
  if (!canvas.width || !canvas.height || !area.clientWidth || !area.clientHeight) return;
  const scale = Math.min(1, area.clientWidth / canvas.width, area.clientHeight / canvas.height);
  // Transform the entire surface (including video/cursor overlays). The
  // canvas keeps its native dimensions; input handlers map pointer locations
  // back into that coordinate space so SPICE mouse positions remain accurate.
  screen.style.width = `${canvas.width}px`;
  screen.style.height = `${canvas.height}px`;
  screen.style.transform = `scale(${scale})`;
  viewport.style.width = `${canvas.width * scale}px`;
  viewport.style.height = `${canvas.height * scale}px`;
}

function requestGuestResize(force = false) {
  if (!connection?.agent_connected || !area.clientWidth || !area.clientHeight) return;
  const width = Math.max(320, Math.floor(area.clientWidth / 8) * 8);
  const height = Math.max(200, Math.floor(area.clientHeight / 8) * 8);
  const size = `${width}x${height}`;
  if (!force && size === lastGuestSize) return;
  connection.resize_window(0, width, height, 32, 0, 0);
  lastGuestSize = size;
}

function resize() {
  fitDisplay();
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(requestGuestResize, 200);
}

async function connect() {
  stop();
  const attempt = generation;
  reconnect.disabled = true;
  disconnect.disabled = false;
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
    if (attempt !== generation) return;
    pending = null;
    if (config.fallback) {
      fallback.href = config.fallback;
      fallback.hidden = false;
    }
    if (!response.ok) throw new Error(config.error || 'Could not open the console.');
    document.getElementById('console-title').textContent = config.title;
    document.title = `${config.title} · AccessForge`;
    const url = new URL(config.websocket, window.location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    connection = new ClipboardSpiceConnection({
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
        lastGuestSize = null;
        resize();
      },
      onclipboardstate(ready) {
        if (attempt !== generation) return;
        // Retry after guest capabilities arrive; an earlier resize may have
        // been sent while the guest agent was still initializing.
        lastGuestSize = null;
        resize();
        clipboardSendButton.disabled = !ready;
        showClipboardStatus(ready ? 'Clipboard ready. Send text to the VM or copy text inside it.' :
          'Clipboard sharing requires a connected SPICE guest agent.');
      },
      onclipboard(text) {
        if (attempt !== generation) return;
        guestClipboard = text;
        clipboardReceive.value = text ?? '';
        clipboardCopy.disabled = text === null;
        if (text !== null) showClipboardStatus('VM text received. Click Copy from VM or select and copy it below.');
      },
      onerror() {
        if (attempt !== generation) return;
        stop();
        showStatus('Connection lost or unavailable. Reconnect, or try noVNC.', true);
      },
    });
    connectTimer = setTimeout(() => {
      if (attempt === generation && !screen.querySelector('canvas')) {
        stop();
        showStatus('The VM display did not become ready. Reconnect, or try noVNC.', true);
      }
    }, 20000);
  } catch (error) {
    if (attempt !== generation || error.name === 'AbortError') return;
    stop();
    showStatus(error.message || 'Could not open the console.', true);
  }
}

reconnect.addEventListener('click', connect);
disconnect.addEventListener('click', () => { stop(); showStatus('Disconnected.'); });
fallback.addEventListener('click', stop);
fitWindow.addEventListener('click', () => {
  clearTimeout(resizeTimer);
  area.scrollTo(0, 0);
  fitDisplay();
  requestGuestResize(true);
  screen.querySelector('canvas')?.focus({ preventScroll: true });
});
keys.addEventListener('click', () => {
  if (connection?.inputs?.state === 'ready') sendCtrlAltDel(connection);
  screen.querySelector('canvas')?.focus();
});
clipboardToggle.addEventListener('click', () => {
  clipboardPanel.hidden = !clipboardPanel.hidden;
  clipboardToggle.setAttribute('aria-expanded', String(!clipboardPanel.hidden));
  if (!clipboardPanel.hidden) clipboardSend.focus();
});
clipboardPaste.addEventListener('click', async () => {
  const attempt = generation;
  const draft = clipboardSend.value;
  clipboardPaste.disabled = true;
  try {
    if (!navigator.clipboard?.readText) throw new Error('Clipboard API unavailable');
    const text = await navigator.clipboard.readText();
    if (attempt !== generation) return;
    if (clipboardSend.value !== draft) {
      showClipboardStatus('The text box changed while clipboard access was pending. Click Paste from computer again to replace it.');
      return;
    }
    clipboardSend.value = text;
    showClipboardStatus('Text loaded. Click Send to VM to share it.');
  } catch {
    if (attempt !== generation) return;
    clipboardSend.focus();
    showClipboardStatus('Browser clipboard access is blocked. Paste into the text box with Ctrl+V or ⌘V, then click Send to VM.', true);
  } finally {
    if (attempt === generation) clipboardPaste.disabled = false;
  }
});
clipboardSendButton.addEventListener('click', () => {
  try {
    if (!connection) throw new Error('Connect to the VM first.');
    connection.offerClipboard(clipboardSend.value);
    showClipboardStatus('Text ready to paste inside the VM.');
  } catch (error) { showClipboardStatus(error.message, true); }
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
    if (attempt === generation && guestClipboard === text) showClipboardStatus('Copied VM text to your computer.');
  } catch {
    if (attempt !== generation || guestClipboard !== text) return;
    clipboardReceive.focus();
    clipboardReceive.select();
    showClipboardStatus('Browser clipboard access is blocked. The VM text is selected; copy it with Ctrl+C or ⌘C.', true);
  }
});
document.getElementById('fullscreen').addEventListener('click', async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch { showStatus('Fullscreen is unavailable in this browser or embedded view.'); }
});
new ResizeObserver(resize).observe(area);
new MutationObserver(fitDisplay).observe(screen, {
  childList: true, subtree: true, attributes: true, attributeFilter: ['width', 'height'],
});
document.addEventListener('fullscreenchange', resize);
window.addEventListener('pagehide', stop);
window.addEventListener('pageshow', event => { if (event.persisted) connect(); });
connect();
