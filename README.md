# AccessForge

AccessForge signs into the Proxmox VE API and opens browser consoles across your cluster. QEMU VMs default to embedded SPICE; containers use Proxmox's xterm.js terminal. Users do not install a native client.

## Features

- Proxmox login and VM listing, grouped by scenario
- Browser SPICE with reconnect, fullscreen, Ctrl+Alt+Del, and a noVNC fallback
- Clipboard panel for two-way text transfer, including browsers that deny clipboard permission
- Container xterm.js consoles
- Cluster-aware SPICE routing; reconnect resolves the VM's current node
- Existing bulk VM actions and scenario backend resets

## Requirements

- Python 3.11+ for a local installation, or Docker Compose on the Proxmox Linux host
- A reachable Proxmox API on TCP 8006
- SPICE-enabled VMs (the same prerequisite as native SPICE)
- AccessForge can reach a Proxmox SPICE proxy on TCP 3128; cluster nodes can reach one another's SPICE proxies
- An HTTPS reverse proxy routing `/spice/ws` to the bridge, included in the supplied deployment
- A modern desktop browser; validate your guest workloads in the browsers you support

No Proxmox packages or patches are required. AccessForge does not change VM display settings. A VM that does not support SPICE can still use the noVNC fallback.

## Docker Compose deployment

Set deployment values in your shell or an untracked `.env` file:

```bash
export PROXMOX_HOST="pve.example.com"
export PROXMOX_REALM="pam"
export VERIFY_SSL="true"
export FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
docker compose up --build -d
```

Open `https://<proxmox-host>/`. The supplied Nginx service uses host networking and serves AccessForge on 443. The certs service creates a self-signed certificate if none exists in `certs/`; install a trusted certificate for regular use.

Nginx forwards application traffic to host loopback port 8080 and `/spice/ws` to host loopback port 8081. Docker publishes both ports only on loopback. Do not expose 8081 publicly; users only need HTTPS. Port 3128 remains internal.

When upgrading an existing installation, rebuild the app **and** reload the Nginx configuration:

```bash
docker compose up --build -d proxclient
docker compose exec nginx-proxmox nginx -t
docker compose restart nginx-proxmox
```

The Compose configuration defaults `PROXMOX_HOST` to the existing deployment hostname, `arlsouth4.utep.edu`. Override it for other installations. If API access uses an address that cannot reach the SPICE proxy, set `SPICE_PROXY_HOST` to the internal address of a cluster node. Inside Docker, `127.0.0.1` refers to the container, not the Proxmox host.

## Local installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PROXMOX_HOST="pve.example.com"
export FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
python main.py
```

Use `python main.py` in production behind the reverse proxy too. It starts Waitress on 8080 and an asynchronous SPICE bridge on 8081 in the same process. Running `waitress main:app` or `flask run` alone does **not** start the bridge. Run one application process per deployment; its console grants are held in memory.

The UI is available at `http://localhost:8080`, but browser SPICE requires a same-origin reverse proxy. Use the `/spice/ws` location in `deploy/proxmox.conf` as a reference. `HTTPS_CERT_FILE`/`HTTPS_KEY_FILE` do not enable TLS in Waitress; terminate TLS at the reverse proxy.

## How browser SPICE connects

```text
Browser -- HTTPS/WebSocket --> AccessForge/Nginx
                                  |
                                  +-- API :8006 --> current VM node and SPICE ticket
                                  |
                                  +-- CONNECT :3128 --> cluster SPICE proxy
                                                          |
                                                          +--> target node / VM (TLS)
```

The server requests `/nodes/{node}/qemu/{vmid}/spiceproxy` with the signed-in user's permissions. The returned routing ticket, cluster CA, and certificate subject stay server-side. The browser receives the short-lived SPICE password and an opaque, session-bound WebSocket grant. Each SPICE channel gets its own tunnel.

The bridge verifies the VM server's certificate chain and exact subject using the API-provided cluster CA, even if API `VERIFY_SSL` is disabled. Grants accept new channels for 25 seconds, before Proxmox's 30-second password expiry. Existing channels stay connected until disconnect, logout, or AccessForge's 110-minute login expiry. Reconnect obtains fresh credentials and resolves the VM's current node. Seamless live migration is not supported; reconnect after migration if the session drops.

The vendored client is pinned in `static/vendor/spice-html5/UPSTREAM.md`; assets are served locally without a CDN. Guest-agent-assisted resizing is supported. Without the guest agent, larger displays scroll at their native resolution. Browser SPICE has upstream feature limits, including multiple displays, USB redirection, and some graphics/video formats; it is not guaranteed to match native-client performance. Keep noVNC available while evaluating your workloads.

## Clipboard

Open **Clipboard** in the console toolbar. Sharing requires a running SPICE guest agent inside the VM; users do not install anything on their own computers.

- **Computer → VM:** paste into the outgoing text box, or click **Paste from computer**, then **Send to VM**. Paste normally inside the guest application. Sending sets the guest clipboard; it does not simulate typing or automatically paste into the focused application.
- **VM → computer:** copy text inside the VM. It appears in the incoming text box. Click **Copy from VM**, or select the text and copy it manually.
- If browser clipboard access is blocked or unavailable, the text boxes still work with normal copy/paste shortcuts. API access generally requires HTTPS and may require browser permission; embedded views can impose additional restrictions.
- Transfers support UTF-8 text, including multiple lines, up to **1 MiB**. Images and files are not supported by this clipboard panel. Large text is reassembled across agent packets.
- Sharing is explicit: focusing the VM never reads or overwrites the computer's clipboard. Guest copies update only the incoming text box until the user clicks **Copy from VM**. Clipboard text is held in the console's memory, cleared on disconnect/reconnect, and is not saved to browser storage or logged by AccessForge.
- If **Send to VM** stays disabled, verify the VM's SPICE agent is connected and supports clipboard sharing. No Proxmox host configuration is changed by this feature.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROXMOX_HOST` | `127.0.0.1` in Python | Cluster API entry point; Compose has a deployment-specific default |
| `PROXMOX_PORT` | `8006` | API port |
| `PROXMOX_REALM` | `pam` | Login realm |
| `VERIFY_SSL` | `false` | Verify the API certificate; enable with a trusted certificate |
| `FLASK_SECRET_KEY` | `change-me-now` | Set a strong secret for signed sessions |
| `PORT` | `8080` | Waitress listener |
| `SPICE_PROXY_HOST` | Signed-in session's Proxmox host | Internal cluster SPICE entry point |
| `SPICE_PROXY_PORT` | `3128` | Internal SPICE proxy port |
| `SPICE_BRIDGE_HOST` | `127.0.0.1` | Bridge bind address; Compose sets `0.0.0.0` inside the container |
| `SPICE_BRIDGE_PORT` | `8081` | Bridge listener; update Nginx too if changed |
| `LOG_LEVEL` | `DEBUG` in Python, `INFO` in Compose | Application logging |
| `DEBUG_HTTP` | `false` | Verbose upstream HTTP debugging; leave off in normal use |
| `DEFAULT_THEME` | `pokemon` | Dashboard theme |

## Permissions and security

- VM listing requires `VM.Audit` on the relevant VM paths; opening consoles requires `VM.Console`.
- SPICE session creation requires the signed Flask session, a console CSRF token, and the matching request origin.
- WebSocket connections require that same signed session and origin, plus a short-lived grant. The browser cannot choose the bridge's TCP destination.
- Limits: 128 console sessions per process, 8 per login ticket, and 16 channels per console.
- SPICE response bodies are redacted from application logs. The Nginx WebSocket route disables access logging so grant URLs are not recorded. Preserve this behavior in any additional proxy.
- Use HTTPS, a strong `FLASK_SECRET_KEY`, and verified API TLS for production.

## Endpoints

- `GET/POST /login` — authenticate against Proxmox
- `GET /` — VM list and console launcher
- `GET/POST /open` — VM SPICE or container xterm.js; `console=novnc` explicitly selects the VM fallback
- `GET /console/spice/<vmid>` — browser SPICE console
- `POST /api/spice/<vmid>/session` — authorized, fresh SPICE connection details
- `/spice/ws` — WebSocket route served by the bridge through Nginx
- `GET /logout` — close SPICE sessions and clear login cookies
- `GET /healthz` — application health and basic configuration

## Troubleshooting and validation

- **`ModuleNotFoundError: No module named 'cryptography'` (or `websockets`):** the container image is missing the new dependencies. Compose bind-mounts the source, so pulling new code or restarting the container can load the new code while keeping the old Python packages. Rebuild and recreate the app; a restart alone does not install dependencies:

  ```bash
  docker compose build --no-cache proxclient
  docker compose up -d --force-recreate proxclient
  docker compose exec proxclient python -c "from cryptography import x509; from websockets.asyncio.server import serve; print('SPICE dependencies OK')"
  ```

  The Dockerfile now fails on installation errors and verifies application imports during the build. If the build fails, resolve that error before recreating the container.
- **Bridge unavailable:** use `python main.py` and confirm the 8081 listener starts. Rebuild the Docker image after dependency changes.
- **WebSocket fails:** reload Nginx with the new `/spice/ws` route; verify AccessForge can reach the configured proxy on 3128 and that inter-node 3128 is allowed. Working API access on 8006 alone does not prove this.
- **VM unavailable:** check that it is running, its display supports SPICE, and the user has `VM.Audit` and `VM.Console`. Try the noVNC link.
- **TLS tunnel failure:** verify API access is reaching the intended cluster and the target node's SPICE certificate matches the returned cluster CA/subject. The bridge does not bypass TLS verification.
- **Resizing does nothing:** verify the guest SPICE agent is installed and running. AccessForge does not install guest software.
- **After migration:** reconnect to look up the VM's new node.

Run the automated suite:

```bash
pip install pytest
python -m pytest -q
```

The suite tests console selection, permissions, migration lookup, grant validation, and real WebSocket-to-CONNECT-to-TLS binary relay using a local simulated proxy. It does not measure real guest performance. Before rollout, test a VM on the AccessForge node, a VM on another node, keyboard/mouse input, guest resizing, reconnect after migration, noVNC fallback, and a container xterm.js session.

An optional browser test runs the actual client against a simulated SPICE guest, including RSA ticket authentication, drawing, keyboard/mouse input, reconnect, and two-way clipboard transfers. Clipboard API permission outcomes are simulated so the tests do not read or modify the developer's real system clipboard:

```bash
pip install playwright
playwright install chromium
RUN_BROWSER_TESTS=1 python -m pytest tests/test_spice_browser.py -q
```

Alternatively, set `PLAYWRIGHT_CHROMIUM_PATH` to an existing Chrome/Chromium executable.
Set `PLAYWRIGHT_BROWSER=firefox` or `PLAYWRIGHT_BROWSER=webkit` to run the same test in those engines after installing them with `playwright install firefox webkit`.

Protocol edge cases (message fragmentation, selection handling, size limits, and agent disconnects) can also be tested without a browser:

```bash
node --test tests/spice_clipboard.test.mjs
```

## License

An application license has not yet been specified. The vendored spice-html5 source retains its upstream copyright notices and `COPYING` / `COPYING.LESSER` files; see its `UPSTREAM.md` for provenance.
