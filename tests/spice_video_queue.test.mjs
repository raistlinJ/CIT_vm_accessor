import test from 'node:test';
import assert from 'node:assert/strict';
import {
  process_video_stream_data,
  handle_append_video_buffer_done,
} from '../static/vendor/spice-html5/src/display.js';

function stream() {
  const appended = [];
  const result = {
    queue: [], start_time: null, cluster_time: 0, append_okay: false,
    waiting_for_keyframe: false,
    video: { buffered: { length: 0 }, paused: false, readyState: 0 },
  };
  result.source_buffer = {
    stream: result,
    appendBuffer(bytes) { appended.push(new Uint8Array(bytes)); },
  };
  return { result, appended };
}

function frame(index, keyframe = false, interval = 33) {
  return {
    base: { multi_media_time: index * interval },
    data: Uint8Array.from([keyframe ? 0 : 1, index]),
  };
}

test('VP8 backlog keeps a recent real keyframe and rebases its WebM cluster', () => {
  const { result, appended } = stream();
  result.id = 1;
  result.max_window_size = 1000;
  result.timeout_ms = 100000;
  result.report = { num_frames: 0, num_drops: 0, start_frame_mm_time: 0 };
  result.media = { spiceconn: {
    streams: [null, result], parent: { relative_now: () => 0 }, send_msg() {},
  } };
  for (let i = 0; i <= 30; i++) {
    process_video_stream_data(result, frame(i, i === 0 || i === 25));
  }

  assert.equal(result.waiting_for_keyframe, false);
  assert.equal(result.cluster_time, 25 * 33);
  assert.equal(result.queue.length, 7);
  assert.equal(result.queue[0].kind, 'cluster');
  assert.equal(result.queue[1].kind, 'frame');
  assert.equal(result.queue[1].keyframe, true);
  assert.equal(result.queue[1].data[1], 25);
  assert.deepEqual(result.queue.slice(2).map(item => item.data[1]), [26, 27, 28, 29, 30]);
  assert.equal(result.report.num_drops, 25);

  handle_append_video_buffer_done.call(result.source_buffer);
  handle_append_video_buffer_done.call(result.source_buffer);
  handle_append_video_buffer_done.call(result.source_buffer);
  assert.equal(appended[0][0], 0x1f); // WebM Cluster
  assert.equal(appended[1][0], 0xa3); // WebM SimpleBlock
  assert.equal(new DataView(appended[2].buffer).getUint16(10), 33);
});

test('VP8 backlog without a recent keyframe waits for the next one', () => {
  const { result } = stream();
  for (let i = 0; i <= 31; i++) {
    process_video_stream_data(result, frame(i, i === 0));
  }
  assert.equal(result.waiting_for_keyframe, true);
  assert.equal(result.queue.length, 0);

  process_video_stream_data(result, frame(32));
  assert.equal(result.queue.length, 0);
  process_video_stream_data(result, frame(33, true));
  assert.equal(result.waiting_for_keyframe, false);
  assert.deepEqual(result.queue.map(item => item.kind), ['cluster', 'frame']);
  assert.equal(result.queue[1].data[1], 33);
});

test('VP8 starts decoding only after its first keyframe', () => {
  const { result } = stream();
  process_video_stream_data(result, frame(0));
  assert.equal(result.waiting_for_keyframe, true);
  assert.equal(result.queue.length, 0);
  process_video_stream_data(result, frame(1, true));
  assert.equal(result.start_time, 33);
  assert.deepEqual(result.queue.map(item => item.kind), ['cluster', 'frame']);
});

test('a timed WebM cluster does not label a VP8 delta as a keyframe', () => {
  const { result } = stream();
  process_video_stream_data(result, frame(0, true));
  process_video_stream_data(result, frame(1, false, 1000));
  assert.deepEqual(result.queue.map(item => item.kind), ['cluster', 'frame', 'cluster', 'frame']);
  assert.equal(new Uint8Array(result.queue[3].mb)[12], 0);
});

test('VP8 queue stays bounded during sustained slow appends', () => {
  const { result } = stream();
  for (let i = 0; i < 200; i++) {
    process_video_stream_data(result, frame(i, i % 20 === 0));
    assert.ok(result.queue.length <= 31);
  }
});

test('large VP8 frames trigger the byte limit before the frame limit', () => {
  const { result } = stream();
  for (let i = 0; i < 10; i++) {
    const msg = frame(i, i === 0 || i === 7);
    msg.data = new Uint8Array(900 * 1024);
    msg.data[0] = i === 0 || i === 7 ? 0 : 1;
    process_video_stream_data(result, msg);
  }
  assert.deepEqual(result.queue.filter(item => item.kind === 'frame')
    .map(item => item.msg_mmtime), [7 * 33, 8 * 33, 9 * 33]);
  assert.ok(result.queue.reduce((bytes, item) => bytes + item.mb.byteLength, 0) < 8 * 1024 * 1024);
});

test('VP8 playback catches up when MediaSource is far behind live video', () => {
  const { result } = stream();
  result.video.buffered = { length: 1, start: () => 0, end: () => 5 };
  result.video.currentTime = 0;
  handle_append_video_buffer_done.call(result.source_buffer);
  assert.ok(result.video.currentTime >= 4.6);

  result.video.currentTime = 4;
  handle_append_video_buffer_done.call(result.source_buffer);
  assert.equal(result.video.currentTime, 4);
});
