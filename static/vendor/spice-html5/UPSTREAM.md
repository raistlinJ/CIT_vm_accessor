# Vendored spice-html5

Source: https://gitlab.freedesktop.org/spice/spice-html5
Commit: `b372aab6e5ab3ce056f486d3bdcd3845f9e34aad` (2026-09-21)

The `src/`, `README`, `TODO`, `COPYING`, and `COPYING.LESSER` files are
copied without modification. No CDN or package download is used at runtime.
Preserve these license files and the individual source copyright notices.
AccessForge's integration lives in `static/spice-console.js`,
`static/spice-clipboard.js`, and `spice_bridge.py`. The clipboard subclass
reassembles agent messages across SPICE packets, handles explicit text sharing,
and replaces the upstream focus-triggered system clipboard access.
