import test from 'node:test';
import assert from 'node:assert/strict';
import { SpiceDisplayConn } from '../static/vendor/spice-html5/src/display.js';
import { SpiceQuic } from '../static/vendor/spice-html5/src/quic.js';
import { Constants } from '../static/vendor/spice-html5/src/enums.js';

function makeDisplay() {
  const drawn = [];
  const display = Object.create(SpiceDisplayConn.prototype);
  display.ops = [];
  display.ops_head = 0;
  display.image_jobs = new Map();
  display.image_jobs_bytes = 0;
  display.next_image_job = 0;
  display.parent = { onbacklog() { drawn.push('overloaded'); } };
  display.schedule_flush = () => {};
  display.cancel_flush = () => {};
  display.schedule_frame = () => {};
  display.log_warn = () => {};
  display.surfaces = { 0: {
    surface_id: 0, format: Constants.SPICE_SURFACE_FMT_32_xRGB,
    canvas: { context: {
      createImageData(width, height) { return { width, height, data: new Uint8ClampedArray(width * height * 4) }; },
      putImageData(image) { drawn.push([...image.data]); },
    } },
  } };
  return { display, drawn };
}

function imageOptions() {
  return {
    base: { surface_id: 0, box: { left: 0, top: 0, right: 1, bottom: 1 } },
    descriptor: { id: 7n, width: 1, height: 1, flags: Constants.SPICE_IMAGE_FLAGS_CACHE_ME },
    has_alpha: false,
  };
}

function bitmap() {
  return { format: Constants.SPICE_BITMAP_FMT_32BIT,
    flags: Constants.SPICE_BITMAP_FLAGS_TOP_DOWN,
    x: 1, y: 1, stride: 4,
    data: new Uint8Array([3, 2, 1, 0]).buffer };
}

function jpegAlphaDrawCopy() {
  const bytes = new ArrayBuffer(110);
  const view = new DataView(bytes);
  view.setUint32(0, 0, true);       // surface
  view.setUint32(13, 1, true);      // destination right
  view.setUint32(17, 1, true);      // destination bottom
  view.setUint32(21, 53, true);    // image offset
  view.setUint32(33, 1, true);      // source right
  view.setUint32(37, 1, true);      // source bottom
  view.setUint8(61, Constants.SPICE_IMAGE_TYPE_JPEG_ALPHA);
  view.setUint32(63, 1, true);      // image width
  view.setUint32(67, 1, true);      // image height
  view.setUint32(72, 1, true);      // JPEG size
  view.setUint32(76, 30, true);     // JPEG + LZ header + one byte
  view.setUint8(81, 0);            // LZ magic, unused by mock worker
  view.setUint32(89, Constants.LZ_IMAGE_TYPE_RGBA);
  view.setUint32(93, 1);           // alpha width
  view.setUint32(97, 1);           // alpha height
  view.setUint32(101, 4);          // alpha stride
  view.setUint32(105, 1);          // top down
  return bytes;
}

test('QUIC parse keeps compressed bytes for deferred decoding', () => {
  const bytes = new ArrayBuffer(8);
  const view = new DataView(bytes);
  view.setUint32(0, 4, true);
  const quic = new SpiceQuic();
  assert.equal(quic.from_dv(view, 0, bytes), 8);
  assert.equal(quic.data.byteLength, 4);
  assert.equal(quic.outptr, undefined);
});

test('image worker decodes bitmap pixels and transfers the result', async () => {
  const originalSelf = globalThis.self;
  let response;
  globalThis.self = { postMessage(message, transfers) { response = { message, transfers }; } };
  try {
    await import('../static/vendor/spice-html5/src/image-worker.js');
    globalThis.self.onmessage({ data: { id: 1, kind: 'bitmap', source: bitmap() } });
    assert.equal(response.message.id, 1);
    assert.deepEqual([...new Uint8ClampedArray(response.message.pixels)], [1, 2, 3, 255]);
    assert.equal(response.transfers[0], response.message.pixels);
  } finally {
    globalThis.self = originalSelf;
  }
});

test('worker result populates cache before later ordered draws', () => {
  const originalWorker = globalThis.Worker;
  const originalImageData = globalThis.ImageData;
  class MockWorker {
    constructor() { MockWorker.instance = this; }
    postMessage(message) { this.request = message; }
    terminate() { this.terminated = true; }
  }
  globalThis.Worker = MockWorker;
  globalThis.ImageData = class {
    constructor(data, width, height) { Object.assign(this, { data, width, height }); }
  };
  try {
    const { display, drawn } = makeDisplay();
    display.draw_copy_async(imageOptions(), 'bitmap', bitmap());
    display.enqueue(() => drawn.push(display.cache[7n]));
    display.flush(Infinity);
    assert.deepEqual(drawn, []);
    assert.equal(MockWorker.instance.request.kind, 'bitmap');
    MockWorker.instance.onmessage({ data: {
      id: MockWorker.instance.request.id, width: 1, height: 1,
      pixels: new Uint8ClampedArray([1, 2, 3, 255]).buffer,
    } });
    display.flush(Infinity);
    assert.deepEqual(drawn[0], [1, 2, 3, 255]);
    assert.equal(drawn[1], display.cache[7n]);
    assert.equal(display.image_jobs.size, 0);
    assert.equal(display.image_jobs_bytes, 0);
    display.stop_image_worker();
    assert.equal(MockWorker.instance.terminated, true);
  } finally {
    globalThis.Worker = originalWorker;
    globalThis.ImageData = originalImageData;
  }
});

test('cache invalidation stays ordered across a delayed image decode', () => {
  const originalWorker = globalThis.Worker;
  const originalImageData = globalThis.ImageData;
  class MockWorker {
    constructor() { MockWorker.instance = this; }
    postMessage(message) { this.request = message; }
    terminate() {}
  }
  globalThis.Worker = MockWorker;
  globalThis.ImageData = class {
    constructor(data, width, height) { Object.assign(this, { data, width, height }); }
  };
  try {
    const { display, drawn } = makeDisplay();
    display.draw_copy_async(imageOptions(), 'bitmap', bitmap());
    const cachedCopy = () => display.draw_copy_helper({
      base: imageOptions().base, has_alpha: false,
      descriptor: { id: 7n, flags: 0 },
      resolve: () => display.cache?.[7n],
    });
    cachedCopy();
    const invalidation = new ArrayBuffer(11);
    const view = new DataView(invalidation);
    view.setUint16(0, 1, true);
    view.setBigUint64(3, 7n, true);
    display.process_channel_message({
      type: Constants.SPICE_MSG_DISPLAY_INVAL_LIST, data: invalidation,
    });
    cachedCopy();
    MockWorker.instance.onmessage({ data: {
      id: MockWorker.instance.request.id, width: 1, height: 1,
      pixels: new Uint8ClampedArray([1, 2, 3, 255]).buffer,
    } });
    display.flush(Infinity);
    assert.deepEqual(drawn, [[1, 2, 3, 255], [1, 2, 3, 255]]);
    assert.equal(display.cache[7n], undefined);
  } finally {
    globalThis.Worker = originalWorker;
    globalThis.ImageData = originalImageData;
  }
});

test('JPEG alpha draw waits for both browser JPEG and worker LZ decoding', () => {
  const originalWorker = globalThis.Worker;
  const originalImage = globalThis.Image;
  const originalImageData = globalThis.ImageData;
  class MockWorker {
    constructor() { MockWorker.instance = this; }
    postMessage(message) { this.request = message; }
    terminate() {}
  }
  class MockImage {
    constructor() { MockImage.instance = this; }
    set src(value) { this.source = value; }
    get src() { return this.source; }
  }
  globalThis.Worker = MockWorker;
  globalThis.Image = MockImage;
  globalThis.ImageData = class {
    constructor(data, width, height) { Object.assign(this, { data, width, height }); }
  };
  try {
    const { display } = makeDisplay();
    display.surfaces[0].format = Constants.SPICE_SURFACE_FMT_32_ARGB;
    assert.equal(display.process_channel_message({
      type: Constants.SPICE_MSG_DISPLAY_DRAW_COPY, data: jpegAlphaDrawCopy(),
    }), true);
    assert.equal(display.ops[0].ready, false);
    MockImage.instance.onload();
    assert.equal(display.ops[0].ready, false);
    MockWorker.instance.onmessage({ data: {
      id: MockWorker.instance.request.id, width: 1, height: 1,
      pixels: new Uint8ClampedArray([255, 255, 255, 255]).buffer,
    } });
    assert.equal(display.ops[0].ready, true);
    assert.equal(typeof display.ops[0].draw, 'function');
  } finally {
    globalThis.Worker = originalWorker;
    globalThis.Image = originalImage;
    globalThis.ImageData = originalImageData;
  }
});

test('worker unavailable uses the decoder and leaves the queue ready', () => {
  const originalWorker = globalThis.Worker;
  globalThis.Worker = undefined;
  try {
    const { display, drawn } = makeDisplay();
    display.draw_copy_async(imageOptions(), 'bitmap', bitmap());
    display.flush(Infinity);
    assert.deepEqual(drawn, [[1, 2, 3, 255]]);
  } finally {
    globalThis.Worker = originalWorker;
  }
});

test('worker failure decodes pending images locally in order', () => {
  const originalWorker = globalThis.Worker;
  class FailingWorker {
    constructor() { FailingWorker.instance = this; }
    postMessage() {}
    terminate() { this.terminated = true; }
  }
  globalThis.Worker = FailingWorker;
  try {
    const { display, drawn } = makeDisplay();
    display.draw_copy_async(imageOptions(), 'bitmap', bitmap());
    display.flush(Infinity);
    assert.deepEqual(drawn, []);
    FailingWorker.instance.onerror({ preventDefault() {} });
    display.flush(Infinity);
    assert.deepEqual(drawn, [[1, 2, 3, 255]]);
    assert.equal(display.image_worker_disabled, true);
    assert.equal(FailingWorker.instance.terminated, true);
  } finally {
    globalThis.Worker = originalWorker;
  }
});

test('render backlog triggers one recovery without dropping ordered draws', async () => {
  const { display, drawn } = makeDisplay();
  for (let i = 0; i < 1025; i++) display.enqueue(() => {});
  await Promise.resolve();
  assert.deepEqual(drawn, ['overloaded']);
  assert.equal(display.ops.length, 1025);
  display.flush(Infinity);
  assert.equal(display.ops.length, 0);
});

test('estimated decoded image bytes trigger recovery before posting', async () => {
  const originalWorker = globalThis.Worker;
  class MockWorker {
    constructor() { MockWorker.instance = this; }
    postMessage() { throw new Error('oversized image was posted'); }
    terminate() {}
  }
  globalThis.Worker = MockWorker;
  try {
    const { display, drawn } = makeDisplay();
    const options = imageOptions();
    options.descriptor.width = 6000;
    options.descriptor.height = 6000;
    display.draw_copy_async(options, 'bitmap', bitmap());
    await Promise.resolve();
    assert.deepEqual(drawn, ['overloaded']);
    assert.equal(display.render_overloaded, true);
  } finally {
    globalThis.Worker = originalWorker;
  }
});

test('oldest queued draw also triggers recovery', async () => {
  const { display, drawn } = makeDisplay();
  display.enqueue(() => {});
  display.ops[0].queued_at -= 4000;
  display.enqueue(() => {});
  await Promise.resolve();
  assert.deepEqual(drawn, ['overloaded']);
  assert.equal(display.process_channel_message({ type: -1 }), true);
});

test('pending decode watchdog recovers without another WebSocket message', async () => {
  const originalWindow = globalThis.window;
  let tick;
  globalThis.window = {
    setTimeout(callback) { tick = callback; return 1; },
    clearTimeout() {},
  };
  try {
    const { display, drawn } = makeDisplay();
    display.enqueue_pending();
    assert.equal(typeof tick, 'function');
    display.ops[0].queued_at -= 4000;
    tick();
    await Promise.resolve();
    assert.deepEqual(drawn, ['overloaded']);
  } finally {
    globalThis.window = originalWindow;
  }
});
