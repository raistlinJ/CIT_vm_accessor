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

Local input fixes:

- `src/spicemsg.js`: encode relative mouse motion as 10 bytes (signed dx/dy
  and a 16-bit button mask), without the absolute-position display-id byte.
  Layout: https://gitlab.com/spice/spice-common/-/blob/master/spice.proto
- `src/spicemsg.js`: map mouse viewport coordinates to native canvas pixels
  for consistent input when the popup scales its display down.
- `src/inputs.js`: explicitly focus the canvas on mouse-down before preventing
  the browser's default action, including after focus leaves without a mouseover.

`tests/test_spice_browser.py` validates input payloads and focus recovery in
real browsers, with both relative and absolute mouse modes.
