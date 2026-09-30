import { SpiceMainConn } from './vendor/spice-html5/src/main.js';
import { Constants as C } from './vendor/spice-html5/src/enums.js';
import { SpiceMsgClipboardGrab, SpiceMsgClipboardRequest, SpiceMsgClipboardSend } from './vendor/spice-html5/src/spicemsg.js';

export const MAX_CLIPBOARD_BYTES = 1024 * 1024;

// Agent messages are a byte stream inside SPICE MAIN_AGENT_DATA packets. A
// packet may contain a partial header, a partial payload, or several messages.
// Reassemble once into a bounded buffer rather than repeatedly copying text.
export class AgentMessageReader {
  constructor() {
    this.header = new Uint8Array(20);
    this.headerUsed = 0;
    this.message = null;
    this.used = 0;
  }

  push(buffer) {
    const bytes = new Uint8Array(buffer);
    const complete = [];
    let offset = 0;
    while (offset < bytes.length) {
      if (!this.message) {
        const count = Math.min(20 - this.headerUsed, bytes.length - offset);
        this.header.set(bytes.subarray(offset, offset + count), this.headerUsed);
        this.headerUsed += count;
        offset += count;
        if (this.headerUsed < 20) continue;
        const header = new DataView(this.header.buffer);
        const size = header.getUint32(16, true);
        if (header.getUint32(0, true) !== C.VD_AGENT_PROTOCOL || size > MAX_CLIPBOARD_BYTES + 64) {
          throw new Error('The VM sent an invalid or oversized agent message.');
        }
        this.message = new Uint8Array(20 + size);
        this.message.set(this.header);
        this.headerUsed = 0;
        this.used = 20;
      }
      const count = Math.min(this.message.length - this.used, bytes.length - offset);
      this.message.set(bytes.subarray(offset, offset + count), this.used);
      this.used += count;
      offset += count;
      if (this.used === this.message.length) {
        complete.push(this.message.buffer);
        this.message = null;
        this.used = 0;
      }
    }
    return complete;
  }
}

// Keep the vendored client unchanged. Override its clipboard behavior here so
// focusing the display cannot read or overwrite the user's system clipboard.
export class ClipboardSpiceConnection extends SpiceMainConn {
  constructor(options) {
    super(options);
    this.onclipboard = options.onclipboard || (() => {});
    this.onclipboardstate = options.onclipboardstate || (() => {});
    this.agentReader = new AgentMessageReader();
    this.localClipboard = null;
    this.guestOwnsClipboard = false;
    this.awaitingClipboard = false;
  }

  get clipboardReady() {
    return Boolean(this.agent_connected && ((this.agent_caps[0] || 0) & (1 << C.VD_AGENT_CAP_CLIPBOARD_BY_DEMAND)));
  }

  resetClipboard() {
    this.agentReader = new AgentMessageReader();
    this.agent_caps = [0];
    this.localClipboard = null;
    this.guestOwnsClipboard = false;
    this.awaitingClipboard = false;
    this.onclipboard(null);
    this.onclipboardstate(false);
  }

  connect_agent() {
    this.resetClipboard();
    super.connect_agent();
  }

  stop() {
    this.resetClipboard();
    super.stop();
  }

  // The upstream display calls this on focus. Sharing is explicit in our UI.
  send_clipboard_grab() {}

  offerClipboard(text) {
    if (!this.clipboardReady) throw new Error('Clipboard sharing requires a connected SPICE guest agent.');
    if (new TextEncoder().encode(text).length > MAX_CLIPBOARD_BYTES) {
      throw new Error('Clipboard text is limited to 1 MiB. Send a smaller selection.');
    }
    this.localClipboard = text;
    this.guestOwnsClipboard = false;
    this.awaitingClipboard = false;
    this.onclipboard(null);
    this.send_agent_message(C.VD_AGENT_CLIPBOARD_GRAB,
      new SpiceMsgClipboardGrab(C.VD_AGENT_CLIPBOARD_UTF8_TEXT, this.agent_caps));
  }

  process_channel_message(msg) {
    if (msg.type === C.SPICE_MSG_MAIN_AGENT_DATA) {
      try {
        for (const data of this.agentReader.push(msg.data)) {
          const type = new DataView(data).getUint32(4, true);
          if ([C.VD_AGENT_CLIPBOARD_GRAB, C.VD_AGENT_CLIPBOARD_REQUEST,
               C.VD_AGENT_CLIPBOARD, C.VD_AGENT_CLIPBOARD_RELEASE].includes(type)) {
            this.processClipboard(type, data.slice(20));
          } else {
            if (type === C.VD_AGENT_ANNOUNCE_CAPABILITIES && data.byteLength < 28) {
              throw new Error('Invalid guest agent capabilities.');
            }
            super.process_channel_message({ ...msg, data });
            if (type === C.VD_AGENT_ANNOUNCE_CAPABILITIES) {
              this.onclipboardstate(this.clipboardReady);
            }
          }
        }
      } catch (error) {
        this.report_error(error);
      }
      return true;
    }
    const handled = super.process_channel_message(msg);
    if (msg.type === C.SPICE_MSG_MAIN_AGENT_DISCONNECTED) this.resetClipboard();
    return handled;
  }

  processClipboard(kind, buffer) {
    if (!this.clipboardReady) return;
    const data = new DataView(buffer);
    const prefix = (this.agent_caps[0] & (1 << C.VD_AGENT_CAP_CLIPBOARD_SELECTION)) ? 4 : 0;
    if (data.byteLength < prefix) throw new Error('Invalid clipboard selection.');
    // Do not confuse Linux PRIMARY (mouse selection) with explicit Copy/Paste.
    if (prefix && data.getUint8(0) !== 0) return;
    if (kind === C.VD_AGENT_CLIPBOARD_RELEASE) {
      this.guestOwnsClipboard = false;
      this.awaitingClipboard = false;
      this.onclipboard(null);
      return;
    }
    if (data.byteLength < prefix + 4) throw new Error('Invalid clipboard message.');
    const type = data.getUint32(prefix, true);
    if (kind === C.VD_AGENT_CLIPBOARD_GRAB) {
      if ((data.byteLength - prefix) % 4) throw new Error('Invalid clipboard formats.');
      this.localClipboard = null;
      this.guestOwnsClipboard = true;
      this.awaitingClipboard = false;
      this.onclipboard(null);
      for (let offset = prefix; offset < data.byteLength; offset += 4) {
        if (data.getUint32(offset, true) === C.VD_AGENT_CLIPBOARD_UTF8_TEXT) {
          this.awaitingClipboard = true;
          this.send_agent_message(C.VD_AGENT_CLIPBOARD_REQUEST,
            new SpiceMsgClipboardRequest(C.VD_AGENT_CLIPBOARD_UTF8_TEXT, this.agent_caps));
          break;
        }
      }
    } else if (kind === C.VD_AGENT_CLIPBOARD_REQUEST) {
      const available = type === C.VD_AGENT_CLIPBOARD_UTF8_TEXT && this.localClipboard !== null;
      this.send_agent_message(C.VD_AGENT_CLIPBOARD, new SpiceMsgClipboardSend(
        available ? C.VD_AGENT_CLIPBOARD_UTF8_TEXT : C.VD_AGENT_CLIPBOARD_NONE,
        available ? this.localClipboard : '', this.agent_caps));
    } else if (kind === C.VD_AGENT_CLIPBOARD && this.guestOwnsClipboard && this.awaitingClipboard) {
      this.awaitingClipboard = false;
      if (type !== C.VD_AGENT_CLIPBOARD_UTF8_TEXT) return;
      const text = new Uint8Array(buffer, prefix + 4);
      if (text.byteLength > MAX_CLIPBOARD_BYTES) throw new Error('Clipboard text exceeds 1 MiB.');
      this.onclipboard(new TextDecoder().decode(text));
    }
  }
}
