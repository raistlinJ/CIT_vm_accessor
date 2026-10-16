# Vendored spice-html5

Source: https://gitlab.freedesktop.org/spice/spice-html5
Commit: `b372aab6e5ab3ce056f486d3bdcd3845f9e34aad` (2026-09-21)

The `src/`, `README`, `TODO`, `COPYING`, and `COPYING.LESSER` files were
copied from this revision, with the local input fixes listed below.
No CDN or package download is used at runtime.
Preserve these license files and the individual source copyright notices.
AccessForge's integration lives in `static/spice-console.js`,
`static/spice-clipboard.js`, and `spice_bridge.py`. The clipboard subclass
reassembles agent messages across SPICE packets, handles explicit text sharing,
and replaces the upstream focus-triggered system clipboard access.

Local changes:

- `src/spiceconn.js` and `src/wire.js`: share an optional byte-count observer
  across console channels for client-side throughput metrics. Disabled metrics
  leave the observer callback unset; payloads are neither copied nor parsed.
- `src/wire.js`: retain unread bytes in their original WebSocket packet and
  copy only complete SPICE messages that span packets; avoid shifting the
  packet queue for every message.
- `src/display.js`: drain the ordered draw queue with a head index and
  occasional compaction instead of shifting the array for every draw.
- `src/display.js`: bound queued VP8 video by frame count, bytes, and age.
  Recover from overload at a VP8 keyframe with a fresh WebM cluster, and
  report dropped frames to the SPICE server. Seek near the MediaSource live
  edge if decoded playback falls more than two seconds behind.
- `src/quic.js`, `src/display.js`, and `src/image-worker.js`: defer QUIC
  decoding until drawing, and decode QUIC, LZ RGB, JPEG alpha LZ, and raw
  bitmap images in a module worker. Transfer decoded pixels back to the
  display channel, then populate the image cache and draw in protocol order.
  Fall back to local decoding if workers are unavailable or fail. Cache
  invalidations also run in draw order so a late result cannot revive an image.
- `src/display.js`, `src/spiceconn.js`, and `static/spice-console.js`: detect
  an ordered draw backlog above 1024 operations or three seconds, or image
  jobs above 128 entries or 128 MiB of estimated input/output. Reconnect to
  discard the stale WebSocket backlog; after two recoveries in a minute,
  stop and suggest noVNC. Browser WebSocket receive buffering itself is not
  directly measurable or bounded by this client.

- `src/spicemsg.js`: encode relative mouse motion as 10 bytes (signed dx/dy
  and a 16-bit button mask), without the absolute-position display-id byte.
  Layout: https://gitlab.com/spice/spice-common/-/blob/master/spice.proto
- `src/spicemsg.js`: map mouse viewport coordinates to native canvas pixels
  for consistent input when the popup scales its display down.
- `src/inputs.js`: explicitly focus the canvas on mouse-down before preventing
  the browser's default action, including after focus leaves without a mouseover.
- `src/spicemsg.js`: encode file-transfer names as UTF-8 and replace protocol
  newlines/NUL so non-ASCII names transfer without changing metadata fields.

`tests/test_spice_browser.py` validates input payloads and focus recovery in
real browsers, with both relative and absolute mouse modes.
