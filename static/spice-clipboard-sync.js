// Optional text clipboard synchronization. Browser access stays in the caller,
// allowing permission failures and delayed reads to be tested without OS access.
export function clipboardAccessHelp({ userAgent, secure, readable, writable }) {
  const docs = 'https://developer.mozilla.org/en-US/docs/Web/API/Clipboard_API#security_considerations';
  if (!secure) return {
    automaticSupported: false,
    message: 'Open this console using HTTPS to enable browser clipboard access. You can still paste or copy manually in the text boxes.',
    url: docs,
  };
  if (/Firefox|FxiOS/.test(userAgent)) return {
    automaticSupported: false,
    message: 'Firefox requires a user action for clipboard access and has no always-allow clipboard setting. Use Paste from computer, then Send to VM, or Copy from VM. For automatic sharing, open this console in Chrome or Edge.',
    url: docs,
  };
  if (/Safari/.test(userAgent) && !/Chrome|Chromium|CriOS|Edg|OPR/.test(userAgent)) return {
    automaticSupported: false,
    message: 'Safari requires a user action for clipboard access and has no always-allow clipboard setting. Use Paste from computer, then Send to VM, or Copy from VM. For automatic sharing, open this console in Chrome or Edge.',
    url: docs,
  };
  if (!readable || !writable) return {
    automaticSupported: false,
    message: 'Browser clipboard access is unavailable here. Paste or copy manually in the text boxes, or open the console directly in Chrome or Edge over HTTPS.',
    url: docs,
  };
  return {
    automaticSupported: true,
    message: 'In Chrome, click the site information icon beside the web address → Site settings → Clipboard → Allow. Reload this console, then turn automatic sharing back on. If this popup hides the address bar, change the permission from the main AccessForge tab on the same site. In an embedded view, open the console in its own window.',
    url: 'https://support.google.com/chrome/answer/114662?hl=en',
  };
}

export class ClipboardSync {
  constructor({ readText, writeText, offerText, isActive, onSend, onCopy, onError }) {
    Object.assign(this, { readText, writeText, offerText, isActive, onSend, onCopy, onError });
    this.epoch = 0;
    this.revision = 0;
    this.toEpoch = 0;
    this.fromEpoch = 0;
    this.reset();
  }

  reset() {
    this.epoch++;
    this.revision++;
    this.toVM = this.fromVM = false;
    this.guestText = this.hostText = this.copiedGuestText = null;
    this.reading = this.writing = null;
  }

  setToVM(enabled) {
    this.toVM = enabled;
    this.toEpoch++;
    if (enabled) return this.poll();
  }

  setFromVM(enabled) {
    this.fromVM = enabled;
    this.fromEpoch++;
    if (enabled) {
      this.copiedGuestText = null;
      return this.copyGuest();
    }
  }

  rememberHost(text) {
    this.revision++;
    this.hostText = text;
  }

  receiveGuest(text) {
    this.revision++;
    this.guestText = text;
    if (text === null) this.copiedGuestText = null;
    if (text !== null) return this.copyGuest();
  }

  async poll() {
    if (!this.toVM || !this.isActive() || this.reading || this.writing) return;
    const token = { epoch: this.epoch, direction: this.toEpoch, revision: this.revision };
    this.reading = token;
    try {
      const text = await this.readText();
      if (token.epoch !== this.epoch || token.direction !== this.toEpoch ||
          token.revision !== this.revision || !this.toVM || !this.isActive()) return;
      if (text !== this.hostText) {
        this.offerText(text);
        this.rememberHost(text);
        this.onSend(text);
      }
    } catch (error) {
      if (token.epoch === this.epoch && token.direction === this.toEpoch && this.toVM) {
        this.setToVM(false);
        this.onError('to', error);
      }
    } finally {
      if (this.reading === token) this.reading = null;
    }
  }

  async copyGuest() {
    if (!this.fromVM || !this.isActive() || this.writing || this.guestText === null ||
        this.guestText === this.copiedGuestText) return;
    const token = { epoch: this.epoch, direction: this.fromEpoch, text: this.guestText };
    this.writing = token;
    // A guest copy supersedes any computer read already in flight.
    this.revision++;
    try {
      await this.writeText(token.text);
      if (token.epoch !== this.epoch) return;
      this.rememberHost(token.text); // Do not echo this write back into the VM.
      if (token.direction !== this.fromEpoch || !this.fromVM) return;
      this.copiedGuestText = token.text;
      if (this.guestText === token.text) this.onCopy(token.text);
    } catch (error) {
      if (token.epoch === this.epoch && token.direction === this.fromEpoch && this.fromVM) {
        this.setFromVM(false);
        this.onError('from', error);
      }
    } finally {
      if (this.writing === token) {
        this.writing = null;
        // Serialize writes, coalescing rapid guest copies to the latest text.
        if (token.epoch === this.epoch) void this.copyGuest();
      }
    }
  }
}
