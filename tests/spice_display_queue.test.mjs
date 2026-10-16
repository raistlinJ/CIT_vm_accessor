import test from 'node:test';
import assert from 'node:assert/strict';
import { SpiceDisplayConn } from '../static/vendor/spice-html5/src/display.js';
import { Constants } from '../static/vendor/spice-html5/src/enums.js';
import { SpiceMsgcMouseMotion } from '../static/vendor/spice-html5/src/spicemsg.js';

function displayQueue() {
  const display = Object.create(SpiceDisplayConn.prototype);
  display.ops = [];
  display.ops_head = 0;
  display.schedule_flush = () => {};
  display.cancel_flush = () => {};
  display.schedule_frame = () => {};
  return display;
}

test('draws stay ordered behind an asynchronous image decode', () => {
  const display = displayQueue();
  const drawn = [];
  display.enqueue(() => drawn.push('before'));
  const image = display.enqueue_pending();
  display.enqueue(() => drawn.push('after'));
  display.flush(Infinity);
  assert.deepEqual(drawn, ['before']);

  display.mark_ready(image, () => drawn.push('image'));
  display.flush(Infinity);
  assert.deepEqual(drawn, ['before', 'image', 'after']);
  assert.equal(display.ops.length, 0);
});

test('a large burst drains and a dropped queue does not replay old draws', () => {
  const display = displayQueue();
  let count = 0;
  for (let i = 0; i < 1000; i++) display.enqueue(() => count++);
  display.flush(Infinity);
  assert.equal(count, 1000);
  display.enqueue(() => count++);
  display.drop_queue();
  display.flush(Infinity);
  assert.equal(count, 1000);
});

test('recreating a primary surface removes the old canvas before fitting mouse coordinates', () => {
  const originalDocument = globalThis.document;
  const children = [];
  const screen = {
    style: {},
    appendChild(canvas) { children.push(canvas); canvas.parentNode = this; },
    removeChild(canvas) {
      children.splice(children.indexOf(canvas), 1);
      canvas.parentNode = null;
    },
  };
  globalThis.document = {
    createElement() {
      return {
        setAttribute(name, value) { if (name === 'width' || name === 'height') this[name] = value; },
        getContext() { return { fillRect() {}, save() {} }; },
      };
    },
    getElementById(id) { return id === 'screen' ? screen : null; },
  };
  try {
    const display = displayQueue();
    display.parent = { screen_id: 'screen', mousex: 320, mousey: 240 };
    display.surfaces = [];
    display.hook_events = () => {};
    display.unhook_events = () => {};
    const create = (width, height) => {
      const bytes = new ArrayBuffer(20);
      const view = new DataView(bytes);
      view.setUint32(4, width, true);
      view.setUint32(8, height, true);
      view.setUint32(12, Constants.SPICE_SURFACE_FMT_32_xRGB, true);
      view.setUint32(16, Constants.SPICE_SURFACE_FLAGS_PRIMARY, true);
      display.process_channel_message({ type: Constants.SPICE_MSG_DISPLAY_SURFACE_CREATE, data: bytes });
    };
    const destroy = () => display.process_channel_message({
      type: Constants.SPICE_MSG_DISPLAY_SURFACE_DESTROY, data: new ArrayBuffer(4),
    });

    create(640, 480);
    const oldCanvas = children[0];
    destroy();
    assert.deepEqual(children, []);
    assert.equal(display.parent.mousex, undefined);
    assert.equal(display.parent.mousey, undefined);
    create(1024, 768);
    assert.equal(children.length, 1);
    assert.notEqual(children[0], oldCanvas);
    assert.equal(children[0].width, 1024);
    assert.equal(children[0].height, 768);
    display.flush(Infinity);
    assert.equal(display.surfaces[0].canvas, children[0]);

    destroy();
    display.flush(Infinity);
    assert.deepEqual(children, []);
    assert.equal(display.surfaces[0], undefined);
  } finally {
    globalThis.document = originalDocument;
  }
});

test('relative mouse motion starts from the new framebuffer without a jump', () => {
  const sc = { buttons_state: 0, mousex: undefined, mousey: undefined };
  const canvas = {
    width: 1024, height: 768,
    getBoundingClientRect: () => ({ left: 10, top: 20, width: 512, height: 384 }),
  };
  const event = (x, y) => ({ currentTarget: canvas, clientX: x, clientY: y });
  const first = new SpiceMsgcMouseMotion(sc, event(266, 212));
  assert.deepEqual([first.x, first.y], [0, 0]);
  assert.deepEqual([sc.mousex, sc.mousey], [512, 384]);
  const second = new SpiceMsgcMouseMotion(sc, event(276, 217));
  assert.deepEqual([second.x, second.y], [20, 10]);
});
