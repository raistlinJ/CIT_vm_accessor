// Guest monitor changes are asynchronous. Keep a request pending until the
// primary surface confirms it, but stop retrying if the guest ignores it.
export class GuestResize {
  constructor({ area, surface, connected, send, retryDelay = 1500, maxAttempts = 3 }) {
    this.area = area;
    this.surface = surface;
    this.connected = connected;
    this.send = send;
    this.retryDelay = retryDelay;
    this.maxAttempts = maxAttempts;
    this.reset();
  }

  reset() {
    clearTimeout(this.timer);
    this.timer = undefined;
    this.target = null;
    this.attempts = 0;
  }

  request(retry = false) {
    if (!this.connected()) return;
    const { width: areaWidth, height: areaHeight } = this.area();
    if (!areaWidth || !areaHeight) return;
    const width = Math.max(320, Math.floor(areaWidth / 8) * 8);
    const height = Math.max(200, Math.floor(areaHeight / 8) * 8);
    const target = `${width}x${height}`;
    if (target !== this.target) {
      clearTimeout(this.timer);
      this.target = target;
      this.attempts = 0;
    } else if (!retry || this.attempts >= this.maxAttempts) {
      return;
    }
    const current = this.surface();
    if (current?.width === width && current?.height === height) {
      clearTimeout(this.timer);
      this.timer = undefined;
      return;
    }
    this.send(width, height);
    this.attempts++;
    if (this.attempts < this.maxAttempts) {
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.request(true), this.retryDelay);
    } else {
      this.timer = undefined;
    }
  }
}
