// Counts existing SPICE WebSocket payloads; never copies or parses their contents.
export class ConsoleNetworkMetrics {
  constructor() {
    this.observer = { record: null };
    this.reset();
  }

  reset(now = performance.now()) {
    this.received = this.sent = this.previousReceived = this.previousSent = 0;
    this.previousTime = now;
  }

  enable(enabled, now = performance.now()) {
    this.reset(now);
    this.observer.record = enabled ? (direction, bytes) => {
      if (direction === 'received') this.received += bytes;
      else this.sent += bytes;
    } : null;
  }

  sample(now = performance.now()) {
    const seconds = (now - this.previousTime) / 1000;
    const result = {
      receivedRate: seconds > 0 ? (this.received - this.previousReceived) / seconds : 0,
      sentRate: seconds > 0 ? (this.sent - this.previousSent) / seconds : 0,
      received: this.received, sent: this.sent,
    };
    this.previousReceived = this.received;
    this.previousSent = this.sent;
    this.previousTime = now;
    return result;
  }
}

export function formatNetworkBytes(bytes) {
  const units = ['B', 'KiB', 'MiB', 'GiB'];
  let unit = 0;
  while (bytes >= 1024 && unit < units.length - 1) { bytes /= 1024; unit++; }
  return `${bytes.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}
