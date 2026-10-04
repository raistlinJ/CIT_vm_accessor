// SPICE represents vertical scrolling as discrete mouse-button clicks.
export class WheelAccumulator {
  constructor() { this.reset(); }
  reset() { this.remainder = 0; this.lastTime = -Infinity; }
  consume(event, speed = 1, pageHeight = 800) {
    const delta = event.deltaY * (event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? pageHeight : 1);
    if (!Number.isFinite(delta) || delta === 0) return 0;
    if (event.timeStamp - this.lastTime > 250 || Math.sign(delta) !== Math.sign(this.remainder)) this.remainder = 0;
    this.lastTime = event.timeStamp;
    this.remainder += delta * speed;
    const steps = Math.trunc(this.remainder / 120);
    this.remainder -= steps * 120;
    // Bound work for unusually large browser deltas.
    return Math.max(-20, Math.min(20, steps));
  }
}
