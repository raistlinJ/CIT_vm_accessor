// Optional text clipboard synchronization. Browser access stays in the caller,
// allowing permission failures and delayed reads to be tested without OS access.
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
