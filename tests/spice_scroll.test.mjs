import test from 'node:test';
import assert from 'node:assert/strict';
import { WheelAccumulator } from '../static/spice-scroll.js';

test('small trackpad events accumulate into a single guest step', () => {
  const wheel = new WheelAccumulator();
  for (let i = 0; i < 23; i++) assert.equal(wheel.consume({ deltaY: 5, timeStamp: i }), 0);
  assert.equal(wheel.consume({ deltaY: 5, timeStamp: 23 }), 1);
});
test('speed, line and page units determine guest steps', () => {
  assert.equal(new WheelAccumulator().consume({ deltaY: 240, timeStamp: 0 }, 0.5), 1);
  assert.equal(new WheelAccumulator().consume({ deltaY: -3, deltaMode: 1, timeStamp: 0 }), -1);
  assert.equal(new WheelAccumulator().consume({ deltaY: 1, deltaMode: 2, timeStamp: 0 }, 1, 600), 5);
});
test('reversal and idle gaps discard previous partial movement; horizontal input does not scroll', () => {
  const wheel = new WheelAccumulator();
  assert.equal(wheel.consume({ deltaY: 100, timeStamp: 0 }), 0);
  assert.equal(wheel.consume({ deltaY: -120, timeStamp: 10 }), -1);
  assert.equal(wheel.consume({ deltaY: 100, timeStamp: 20 }), 0);
  assert.equal(wheel.consume({ deltaY: 20, timeStamp: 500 }), 0);
  assert.equal(wheel.consume({ deltaY: 0, timeStamp: 501 }), 0);
});
