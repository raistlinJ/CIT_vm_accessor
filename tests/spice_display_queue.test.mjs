import test from 'node:test';
import assert from 'node:assert/strict';
import { SpiceDisplayConn } from '../static/vendor/spice-html5/src/display.js';
import { Constants } from '../static/vendor/spice-html5/src/enums.js';
import { SpiceMsgcMouseMotion, SpiceMsgcMousePosition, SpiceMsgDisplayMonitorsConfig } from '../static/vendor/spice-html5/src/spicemsg.js';

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

test('monitor geometry selects the visible head and maps absolute input locally', () => {
  const bytes = new ArrayBuffer(4 + 2 * 28);
  const view = new DataView(bytes);
  view.setUint16(0, 2, true);
  const head = (at, id, surface, width, height, x, y) => {
    view.setUint32(at, id, true);
    view.setUint32(at + 4, surface, true);
    view.setUint32(at + 8, width, true);
    view.setUint32(at + 12, height, true);
    view.setInt32(at + 16, x, true);
    view.setInt32(at + 20, y, true);
  };
  head(4, 9, 8, 100, 100, 0, 0);
  head(32, 3, 0, 400, 300, 80, 40);
  assert.equal(new SpiceMsgDisplayMonitorsConfig(bytes).heads.length, 2);

  let changed = 0;
  const display = displayQueue();
  const canvas = {
    width: 640, height: 480,
    getBoundingClientRect: () => ({ left: 20, top: 30, width: 320, height: 240 }),
  };
  display.surfaces = { 0: { surface_id: 0, width: 640, height: 480, canvas } };
  display.primary_surface = 0;
  display.parent = { mousex: 17, mousey: 20, onmonitorchange() { changed++; } };
  assert.equal(display.process_channel_message({ type: Constants.SPICE_MSG_DISPLAY_MONITORS_CONFIG, data: bytes }), true);
  assert.equal(canvas.spiceMonitor.id, 3);
  assert.deepEqual([canvas.spiceMonitor.x, canvas.spiceMonitor.y, canvas.spiceMonitor.width, canvas.spiceMonitor.height], [80, 40, 400, 300]);
  assert.deepEqual([display.parent.mousex, display.parent.mousey, changed], [undefined, undefined, 1]);

  const sc = { buttons_state: 0 };
  const position = new SpiceMsgcMousePosition(sc, { currentTarget: canvas, clientX: 20 + (80 + 125) / 2, clientY: 30 + (40 + 75) / 2 });
  assert.deepEqual([position.x, position.y, position.display_id], [125, 75, 3]);
  const edge = new SpiceMsgcMousePosition(sc, { currentTarget: canvas, clientX: 20 + 600 / 2, clientY: 30 + 450 / 2 });
  assert.deepEqual([edge.x, edge.y], [399, 299]);
});

test('truncated monitor config is ignored without changing the display', () => {
  const display = displayQueue();
  let warned = 0;
  display.log_warn = () => warned++;
  display.parent = {};
  const bytes = new ArrayBuffer(4);
  new DataView(bytes).setUint16(0, 1, true);
  assert.equal(display.process_channel_message({ type: Constants.SPICE_MSG_DISPLAY_MONITORS_CONFIG, data: bytes }), true);
  assert.equal(warned, 1);
});
