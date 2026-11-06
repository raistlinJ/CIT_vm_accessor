# AccessForge

AccessForge signs into the Proxmox VE API and opens browser consoles across your cluster. QEMU VMs automatically use embedded SPICE when their display is SPICE/QXL and clipboard is Default; other VMs use noVNC. Containers use Proxmox's xterm.js terminal. Users do not install a native client.

## Features

- Proxmox login and VM listing, grouped by scenario
- Materials links grouped into category dropdowns with each project's VMs
- Browser SPICE with Re-Fit Window, Ctrl+Alt+Del, and a noVNC fallback
- Clipboard panel for two-way text transfer, including browsers that deny clipboard permission
- Optional browser-to-VM uploads and VM-to-browser downloads
- Container xterm.js consoles
- Cluster-aware SPICE routing; reconnect resolves the VM's current node
- Existing bulk VM actions and scenario backend resets
- **Restart Selected** gracefully reboots checked, running VMs and containers; stopped guests are skipped

## Requirements

- Python 3.11+ for a local installation, or Docker Compose on the Proxmox Linux host
- A reachable Proxmox API on TCP 8006
- SPICE-enabled VMs (the same prerequisite as native SPICE)
- AccessForge can reach a Proxmox SPICE proxy on TCP 3128; cluster nodes can reach one another's SPICE proxies
- An HTTPS reverse proxy routing `/spice/ws` to the bridge, included in the supplied deployment
- A modern desktop browser; validate your guest workloads in the browsers you support
- For uploads, a working SPICE guest agent; for downloads, an enabled QEMU guest agent and file-read permission (`VM.Monitor` on Proxmox 8, `VM.GuestAgent.FileRead` on Proxmox 9)

No Proxmox packages or patches are required. AccessForge does not change VM display settings. VMs without SPICE/QXL and Default clipboard settings automatically use noVNC.

## Docker Compose deployment

Copy [`.env.example`](.env.example) to `.env` for documented connection settings, optional account checkout, email verification delivery, file transfers, and other deployment options. Replace its host and secret placeholders, then start Compose. Existing shell environment variables take precedence over `.env`.

Alternatively, set deployment values in your shell:

```bash
export PROXMOX_HOST="pve.example.com"
export PROXMOX_REALM="pam"
export VERIFY_SSL="true"
export FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
docker compose up --build -d
```

Open `https://<proxmox-host>/`. The supplied Nginx service uses host networking and serves AccessForge on 443. The certs service creates a self-signed certificate if none exists in `certs/`; install a trusted certificate for regular use.

Nginx forwards application traffic to host loopback port 8080 and `/spice/ws` to host loopback port 8081. Docker publishes both ports only on loopback. Do not expose 8081 publicly; users only need HTTPS. Port 3128 remains internal.

Compose sets `PUBLIC_SCHEME=https` so Waitress knows the browser uses HTTPS even though Nginx forwards plain HTTP. Nginx preserves the browser's host and port. Both hostname and IP URLs work; the Proxmox API address entered at login is independent of the browser URL.

The proxy image is pinned to Nginx 1.30.5, which includes the fix for
[CVE-2026-42533](https://nginx.org/en/security_advisories.html) (fixed in stable
1.30.4 and later). Keep this pin current when applying future security updates.

When upgrading an existing installation, pull the pinned proxy image, rebuild
the app, and recreate the proxy so both the image and configuration are updated.
Restarting the old container alone does not update its image. Recreating the
proxy briefly interrupts HTTPS access:

```bash
docker compose pull nginx-proxmox
docker compose up --build -d proxclient
docker compose up -d --no-deps --force-recreate nginx-proxmox
docker compose exec nginx-proxmox nginx -t
docker compose exec nginx-proxmox nginx -v
```

Verify that the running proxy reports `nginx/1.30.5`, then rerun the vulnerability
scan against the same host and port. Updating this repository alone does not
patch a running deployment.

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

The UI is available at `http://localhost:8080`, but browser SPICE requires a same-origin reverse proxy. Use the `/spice/ws` location in `deploy/proxmox.conf` as a reference. When deploying behind an HTTPS-only proxy, set `PUBLIC_SCHEME=https`. `HTTPS_CERT_FILE`/`HTTPS_KEY_FILE` do not enable TLS in Waitress; terminate TLS at the reverse proxy.

## How browser SPICE connects

The SPICE controls drawer includes a **Scrolling** section, collapsed by default, with **Scroll speed** (Very slow, Slow, Normal, Fast), saved in your browser across VMs. Small trackpad movements accumulate into guest scroll steps instead of sending a full step for every browser event. Choose Slow or Very slow to reduce sensitivity.

When opening a VM, AccessForge resolves its current cluster node and reads its current configuration (excluding pending changes). Displays `qxl`, `qxl2`, `qxl3`, and `qxl4` with no `clipboard` override select SPICE. `clipboard=vnc`, other display types, or unavailable configuration select noVNC. Detection uses the signed-in user's permissions; users without `VM.Audit` configuration access fall back to noVNC. The explicit **Use noVNC** link bypasses detection. Clipboard transfer itself still requires a working guest agent.

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

The bridge verifies the VM server's certificate chain and exact subject using the API-provided cluster CA, even if API `VERIFY_SSL` is disabled. Grants accept new channels for 25 seconds, before Proxmox's 30-second password expiry. AccessForge imposes no fixed login or established-channel age limit. Existing channels stay connected until disconnect or explicit logout. The reverse proxy retains its 60-minute transport inactivity timeouts; these measure gaps in network traffic, not keyboard/mouse inactivity, and WebSocket keepalive traffic normally prevents them on healthy connections. Proxmox still controls its own ticket validity, so API actions and reconnects may require signing in again if the upstream ticket expires; AccessForge does not renew tickets automatically. **Re-Fit Window** obtains fresh SPICE credentials and resolves the VM's current node. Seamless live migration is not supported; use **Re-Fit Window** after migration if the session drops.

The vendored client is pinned in `static/vendor/spice-html5/UPSTREAM.md`; assets are served locally without a CDN. Console controls are tucked into a drawer, opened with the tab on the left edge. The drawer overlays the display without changing the guest resolution; close it with the tab or press Escape while focused inside it. Connection, power-action, and clipboard errors turn the drawer tab red and add an exclamation badge; open the drawer for details. The badge clears when the reported problems are resolved. The display automatically fits inside the popup while preserving its aspect ratio, including when the guest agent cannot resize the desktop. With an agent connected, AccessForge requests a matching guest resolution when the browser window changes. **Re-Fit Window** reconnects the SPICE session, recalculates the fit, and requests a matching guest resolution. Browser SPICE has upstream feature limits, including multiple displays, USB redirection, and some graphics/video formats; it is not guaranteed to match native-client performance. Keep noVNC available while evaluating your workloads.

## VM power controls

The SPICE drawer includes **Start VM** and **Restart VM**. Restart requests a graceful Proxmox reboot, which applies pending VM configuration changes. Actions use the signed-in user's `VM.PowerMgmt` permission and resolve the VM's current cluster node. The drawer shows task progress and reconnects the console after completion, unless you manually reconnect while the task runs. If an action fails or takes longer than three minutes, check its status in Proxmox before retrying. When connecting to a stopped VM, the display explains that the machine is off and directs you to **Start VM**.

The popup's **Start VM** and **Restart VM** buttons first show a confirmation dialog explaining that the action can take at least 60 seconds. **Cancel** sends no request. Confirming sends the request and starts a shared 10-second cooldown; the buttons stay disabled while the power task is pending.

## Screenshots

Use **Take screenshot** in the controls drawer to download a PNG of the VM display at its full guest resolution, even when the popup is scaled down. The filename includes the VM ID and UTC timestamp. Screenshots are generated locally in the browser and exclude the controls drawer and mouse pointer.

**Network Activity** in the SPICE controls drawer offers optional received/sent throughput, byte totals, and a miniature graph of the latest 60 samples. It is off by default; the browser remembers your choice. Counters cover existing SPICE WebSocket payloads across all channels, including SPICE uploads, and update once per second without polling Proxmox or parsing traffic. They exclude VM network interface traffic, HTTP file downloads, and WebSocket/TLS overhead. Totals start when enabled and reset on reconnect. Disabling removes the counting callback and update timer.

## Clipboard

Open the collapsible **Clipboard** section in the left-edge controls drawer to share text. Sharing requires a running SPICE guest agent inside the VM; users do not install anything on their own computers.

- **Computer → VM:** paste into the outgoing text box, or click **Paste from computer**, then **Send to VM**. Paste normally inside the guest application. Sending sets the guest clipboard; it does not simulate typing or automatically paste into the focused application.
- **VM → computer:** copy text inside the VM. It appears in the incoming text box. Click **Copy from VM**, or select the text and copy it manually.
- If browser clipboard access is blocked or unavailable, the text boxes still work with normal copy/paste shortcuts. API access generally requires HTTPS and may require browser permission; embedded views can impose additional restrictions.
- Transfers support UTF-8 text, including multiple lines, up to **1 MiB**. Images and files are not supported by this clipboard panel. Large text is reassembled across agent packets.
- Sharing is explicit: focusing the VM never reads or overwrites the computer's clipboard. Guest copies update only the incoming text box until the user clicks **Copy from VM**. Clipboard text is held in the console's memory, cleared on disconnect/reconnect, and is not saved to browser storage or logged by AccessForge.
- If **Send to VM** stays disabled, verify the VM's SPICE agent is connected and supports clipboard sharing. No Proxmox host configuration is changed by this feature.

## Material categories

Add an optional `"Material Category"` and `materials` list inside the existing `AccessForge` object in a visible VM's Proxmox Notes, at the same level as `file_upload` and `file_download`:

```json
{
  "Scenario": "network-lab",
  "User": "student",
  "Pass": "your-existing-guest-password",
  "AccessForge": {
    "file_upload": false,
    "file_download": false,
    "Material Category": "Network Fundamentals",
    "materials": [
      {"title": "Lab instructions", "url": "https://example.edu/labs/networking"},
      {"title": "Reference PDF", "url": "https://example.edu/files/reference.pdf"}
    ]
  }
}
```

Extend the existing `AccessForge` object rather than adding a second one, and retain the current credentials and transfer settings. These fields can also live in a separate `AccessForge` JSON object alongside the legacy credential JSON, provided there is only one `AccessForge` object in the Notes.

`Scenario` groups VMs, provides the project heading, and identifies backend reset actions. `Material Category` labels each link's category in the Materials table. Links from visible VMs in the same category are combined and listed together, with one merged category cell spanning their separate material rows. Missing, blank, or invalid categories use `Misc`.

Materials appear in a compact table to the left of each project's VMs, stacking above them on narrow screens. Long lists scroll within the panel, with column headings kept visible; keyboard users can focus the panel to scroll it. Links are collected only from visible VMs in that Scenario and deduplicated by URL within each category, using the title from the lowest-ID VM when duplicated. Only absolute HTTP/HTTPS links are accepted. `title` is optional and defaults to the URL. Links open in a new tab; PDFs and other hosted files follow the destination server's viewing/download behavior. AccessForge does not host or fetch these files, and VM file-transfer switches do not affect these links. Empty or invalid metadata shows no Materials section. Reload the page after editing Notes; **Refresh Status** updates only VM and backend status.

## Student account checkout

Set `ENABLE_ACCOUNT_CHECKOUT=true` in AccessForge's environment (Compose forwards it from `.env`) and use a strong, stable `FLASK_SECRET_KEY`. Recreate the app container after changing these values. The student login page then links to `/checkout`; `/admin/checkouts` provides administration. After an administrator signs in, a **Management** link appears in the top menu. It is shown only after verifying `Sys.Modify` on `/` in the configured Proxmox cluster and stays hidden for students. AccessForge owns the published credential pools, secret validation, email verification, assignments, and release. Each SCE-web project has its own checkout pool and a distinct lab secret. A student can use the same email to hold one account in each project; entering that project’s secret recovers its existing account. Students can choose **Check out another project** after receiving credentials, or **Get lab access** from the top menu after signing in. This keeps earlier reservations intact and requires the next project’s secret. Student checkout works without SCE-web being online, and AccessForge never calls SCE-web.

Enable `ACCESSFORGE_PLUGIN_ENABLE=1` in SCE-web, then configure an AccessForge administrator connection in its **Credential Checkouts** tab. Select and publish saved project accounts to this server. SCE-web authenticates with an administrator account using HTTPS; AccessForge validates it against its configured Proxmox cluster and requires `Sys.Modify` on `/`. Administrator usernames without a realm default to `@pam`, independently of the student login realm; explicit realms such as `@pve` or `@ldap` are preserved. Publish only credentials provisioned on that cluster. The integration supports password authentication; accounts requiring an interactive MFA exchange cannot be used as saved SCE-web connections. Ordinary student accounts cannot access administration.

Visit `/admin/checkouts` and sign in using the existing Proxmox login. The administrator chooses a published pool, sets a lab secret of at least eight characters, chooses whether email verification is required, enables checkout, and selects accounts to make available. Publishing alone does not enable checkout. The same page supports manual email assignment, release, and removal of unassigned accounts. SCE-web administrators can use its Credential Checkouts tab to request status, set the lab secret and checkout/email-verification policy through **Checkout settings**, and relinquish selected accounts through **Accounts → Relinquish**. AccessForge remains the source of truth and never calls SCE-web.

Students enter their name (1–120 characters), email, and lab secret at `/checkout`. The name is saved with the assignment, and recovery keeps the original name. Existing assignments without a name gain one on recovery. Required verification uses an eight-digit email code that expires in ten minutes and allows six incorrect attempts. A matching email recovers its existing account rather than consuming another. With verification disabled, the shared lab secret and the email alone are sufficient to claim or recover credentials. Logging out does not release assignments. The result page offers copy controls and **Open lab** using this AccessForge server's configured Proxmox connection. Signed-in students with a current assignment also see **Relinquish account**. Its confirmation warns that they will lose all their work and access; accepting disables the Proxmox login, releases the assignment, closes registered AccessForge SPICE connections, and signs them out. The password and VM contents are unchanged. Credentials and lab secrets are not saved in browser storage or Flask session cookies; responses disable caching.

Email delivery belongs to this AccessForge server:

```dotenv
ENABLE_ACCOUNT_CHECKOUT=true
FLASK_SECRET_KEY=<strong-stable-secret>
CHECKOUT_SMTP_HOST=smtp.example.edu
CHECKOUT_SMTP_PORT=587
CHECKOUT_SMTP_FROM=labs@example.edu
CHECKOUT_SMTP_USER=labs@example.edu
CHECKOUT_SMTP_PASSWORD=<smtp-password>
CHECKOUT_SMTP_SSL=0
```

SMTP uses verified TLS: port 587 with STARTTLS by default, or `CHECKOUT_SMTP_SSL=1` and port 465 for implicit TLS. Required verification fails closed when SMTP is unavailable. SMTP is not needed for pools with verification disabled.

Checkout and relinquishment timestamps and client IP addresses remain available after an assignment is released. Both admin tables display the current checkout name alongside the email. Release clears the current name and email; the persistent events table retains the name with the checkout/release history. The admin table and SCE-web status API expose the latest timestamp/IP values. Recovery of an existing assignment does not overwrite its original checkout time or IP. Administrator releases also record their completion time and IP. Existing records keep their original assignment time; unknown historical IPs display as unavailable.

Client IPs normally use the socket peer. Set `CHECKOUT_TRUSTED_PROXIES` to comma-separated proxy IPs/CIDRs to accept `X-Real-IP` from those peers only. Other forwarded headers are ignored. The supplied Nginx ingress overwrites `X-Real-IP`, and Compose trusts loopback plus its Docker bridge range while exposing the app port only on host loopback. For custom networking, set the actual proxy CIDRs and keep the application port private.

Assignments and encrypted published credentials persist in `CHECKOUT_DATA_DIR/account_checkout.sqlite3`, defaulting to `data/` beside the application. Compose mounts `./data` at `/app/data`; the `checkout-data` initialization service prepares its ownership for the non-root application user (UID/GID 1000) before the app starts. Existing data stays in the same host directory. Back up this directory and `FLASK_SECRET_KEY` together. SQLite transactions prevent simultaneous students receiving the same account. A published Proxmox username belongs to one pool per AccessForge server. Admin status APIs omit passwords and lab-secret hashes.

**Accounts → Relinquish** is available to administrators in both AccessForge and SCE-web. After confirmation, it disables the Proxmox login before freeing the assignment. Failed releases remain reserved for retry. Both admin tables show **Disabled**, and the account cannot be checked out or signed into AccessForge until explicitly re-enabled through SCE-web. The **Mark Credential Type → Mark On-Demand** setting does not re-enable a disabled login. **Mark Permanent** stops new checkouts while retaining existing assignments. AccessForge groups **Assign**, **Relinquish**, and **Remove** under **Accounts**. Registered AccessForge sessions for a released assignment are invalidated and their SPICE connections are closed. Previously issued upstream Proxmox tickets or external/noVNC consoles can remain active until expiration/disconnection. VM contents and credentials in other integrations are not reset.

Automatic disabling requires `User.Modify` privileges. Publishing from SCE-web saves its administrator credential encrypted in the AccessForge pool, allowing local relinquishment when SCE-web is offline. Existing pools must be published again, or an administrator can save their **Proxmox administrator password** on `/admin/checkouts`. This is the password for the signed-in Proxmox administrator, not the lab secret; SCE-web normally saves it automatically when marking accounts On-Demand. Alternatively, set `CHECKOUT_PROXMOX_API_TOKEN=user@realm!token-id=secret` to a dedicated Proxmox token with `User.Modify` on the managed users; publishing then does not save an administrator password here. Tokens support disabling only; SCE-web uses its administrator password connection for explicit password resets. Keep login-management credentials current after changing the administrator password.

To prepare released accounts in SCE-web, save their desired passwords in Configuration, select them in **Credential Checkouts**, and use **Reset selected passwords**. This updates Proxmox and AccessForge's encrypted credentials while leaving logins disabled. When ready, select **Re-enable selected logins**. Accounts then become available according to their existing AccessForge checkout policy. **Disable selected logins** also supports unassigned published accounts. Reset and re-enable operations reject assigned accounts; release them first. Publishing alone never re-enables a disabled account. A changed source password cannot replace an assigned account. The former `SCE_CHECKOUT_URL` / `SCE_CHECKOUT_TOKEN` service connection is removed; all integration requests go from SCE-web into AccessForge.

## VM file transfer

Set `ENABLE_VM_FILE_UPLOAD=true` to send files from the user's browser computer to the VM, `ENABLE_VM_FILE_DOWNLOAD=true` to retrieve files from the VM, or both. Set them in Compose or the Compose `.env` file and recreate the app container. Both default to `false`; when both directions are disabled, the drawer shows a nonexpandable **File Transfer — Disabled** row. Otherwise, open the collapsible **File Transfer** section to see the allowed controls. The app container does not read files from the Proxmox host filesystem. The former `ENABLE_VM_FILE_TRANSFER` setting remains a fallback for either direction that does not have its own setting, so existing deployments keep their behavior.

Uploads use the existing SPICE guest-agent file transfer protocol. The agent decides where transferred files are saved in the guest; the browser shows progress and completion. Uploads are limited to 1 GiB (1,024 MiB) per file.

Both directions also require an explicit per-VM policy in Proxmox Notes, managed by SCE-web:

```json
{"AccessForge": {"file_upload": true, "file_download": false}}
```

Global Compose switches are the final authority: VM notes cannot enable a globally disabled direction. Missing, duplicate, malformed, or unreadable policy disables transfers. The console session response controls the popup's enabled controls. Downloads reread the policy before every file chunk. The SPICE bridge inspects client messages, checks policy before upload starts and periodically during upload traffic (a one-second recheck interval), and closes the connection if the policy denies transfer. A modified browser client cannot bypass this by re-enabling the file input. Reconnect after changing an initially disabled upload policy to enabled.

SCE-web manages VM policies through its existing Proxmox connection and does not need this app's URL or contact it. Its controls and arrows show VM policy; actual transfers still require the corresponding global Compose switch here. `GET /api/file-transfer/capabilities` exposes the global upload/download booleans for diagnostics. This policy governs AccessForge transfers; grant VM-note editing only to users authorized to change the policy. Clipboard text and external clients with their own Proxmox access are separate capabilities.

Downloads use Proxmox's QEMU guest-agent `file-read` API. Enable the QEMU guest agent in the VM's Proxmox options, install and start it inside the guest, and grant the AccessForge login file-read permission (`VM.Monitor` on Proxmox 8; `VM.GuestAgent.FileRead` or `VM.GuestAgent.Unrestricted` on Proxmox 9). Enter an absolute path *inside the guest*, such as `/home/user/report.txt` or `C:\Users\user\report.txt`. The guest agent must be able to read that file. Proxmox may run it with elevated guest privileges, so grant file-read permission only to users authorized to retrieve guest files.

AccessForge first requests 1 MiB chunks using Proxmox's newer `count`, `offset`, and `decode` parameters; downloads are limited to 1 GiB (1,024 MiB) on nodes that support them. If the node rejects those parameters with HTTP 400, AccessForge retries the older `file`-only API, which reads the whole file at once and is limited to 16 MiB. A larger file on an older node produces a size-limit error instead of a partial download. File data is held in the browser until the download starts and is not stored by AccessForge. [Proxmox guest-agent API](https://github.com/proxmox/qemu-server/blob/master/src/PVE/API2/Qemu/Agent.pm), [Proxmox file-read compatibility changes](https://lore.proxmox.com/pve-devel/20260226123122.60418-1-info@ebner-markus.de/), [SPICE file transfer](https://www.spice-space.org/api/spice-gtk/SpiceFileTransferTask.html).

If a download fails, confirm the VM is running, the QEMU guest agent is enabled and responding, the absolute path exists inside the guest, and the login has the permission above. For a browser `502`, inspect the upstream status with `docker compose logs --since=10m proxclient | grep -E 'agent/file-read|VM file-read failed'` on the AccessForge server. AccessForge logs the Proxmox HTTP status while redacting the requested path and file contents. A `403` indicates a permission problem; an upstream `400` is retried through the older API; another upstream error should be checked against the QEMU guest agent and Proxmox logs on the node hosting the VM.

The upload switch hides the upload control. The download switch hides the download control and blocks AccessForge's file-read route when false. Neither switch changes Proxmox or the guest agent's own file-transfer policies.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROXMOX_HOST` | `127.0.0.1` in Python | Cluster API entry point; Compose has a deployment-specific default |
| `PROXMOX_PORT` | `8006` | API port |
| `PROXMOX_REALM` | `pam` | Login realm |
| `VERIFY_SSL` | `false` | Verify the API certificate; enable with a trusted certificate |
| `FLASK_SECRET_KEY` | `change-me-now` | Set a strong secret for signed sessions |
| `PORT` | `8080` | Waitress listener |
| `PUBLIC_SCHEME` | `http` in Python, `https` in Compose | Browser-facing scheme for origin validation and generated URLs; does not enable TLS in Waitress |
| `SPICE_PROXY_HOST` | Signed-in session's Proxmox host | Internal cluster SPICE entry point |
| `SPICE_PROXY_PORT` | `3128` | Internal SPICE proxy port |
| `SPICE_BRIDGE_HOST` | `127.0.0.1` | Bridge bind address; Compose sets `0.0.0.0` inside the container |
| `SPICE_BRIDGE_PORT` | `8081` | Bridge listener; update Nginx too if changed |
| `ENABLE_VM_FILE_UPLOAD` | `false` | Show the browser-to-VM upload control when `true` |
| `ENABLE_VM_FILE_DOWNLOAD` | `false` | Show the VM-to-browser download control and enable the file-read route when `true` |
| `ENABLE_VM_FILE_TRANSFER` | `false` | Legacy fallback for either direction without its own setting |
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
- `GET/POST /open` — automatically selects VM SPICE/noVNC from display and clipboard settings, or container xterm.js; `console=novnc` explicitly selects the VM fallback
- `GET /console/spice/<vmid>` — browser SPICE console
- `POST /api/spice/<vmid>/session` — authorized, fresh SPICE connection details
- `POST /api/spice/<vmid>/power` — start or gracefully restart a VM
- `GET /api/spice/<vmid>/power-task` — poll a signed, session-bound power task
- `POST /api/spice/<vmid>/file-read` — read a bounded VM file chunk using QEMU guest-agent permission; disabled by default
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
- **Permission denied for `/app/data`:** this is a container filesystem permission problem, independent of Proxmox/PAM privileges. Update Compose and run `docker compose up -d --build proxclient` so `checkout-data` prepares the dedicated bind mount before startup. For an older Compose file, repair only the checkout directory from the AccessForge project root with `sudo mkdir -p data`, `sudo chown -Rh 1000:1000 data`, and `sudo chmod 700 data`, then recreate the app. Do not change ownership of the entire project or run the application as root. For custom mounts or user mappings, grant the actual app UID access to `CHECKOUT_DATA_DIR`.
- **SCE-web cannot connect to AccessForge:** enter the AccessForge HTTPS base URL in SCE-web's **Connection** dialog. If AccessForge uses a self-signed certificate, uncheck **Verify SSL certificates** there or install a certificate trusted by the SCE-web server. Accepting the certificate in your browser does not trust it in SCE-web's container. AccessForge's `VERIFY_SSL` instead controls its connection to Proxmox. Other connection failures require checking DNS, the HTTPS port, and reachability from the SCE-web container. Linux sudo access does not grant Proxmox API privileges; checkout administration requires `Sys.Modify` on `/` on AccessForge's configured cluster.
- **Bridge unavailable:** use `python main.py` and confirm the 8081 listener starts. Rebuild the Docker image after dependency changes.
- **Invalid request origin:** for the supplied HTTPS ingress, confirm `PUBLIC_SCHEME=https` in the app container and reload the current Nginx config, which preserves the public host/port. Waitress strips untrusted forwarding headers by default; this deployment sets its URL scheme explicitly. The browser origin must match the application's public scheme, host, and port, not the Proxmox API hostname entered at login. After updating Compose, recreate the app to apply environment changes.

  If it persists, reproduce once and run `docker compose logs --since=2m proxclient`. The `SPICE origin rejected` warning shows `expected` (the address Flask sees) and `received` (the browser's Origin), without session credentials. A scheme mismatch means checking the running container's `PUBLIC_SCHEME` and browser URL; a host/port mismatch means checking each reverse proxy's Host forwarding. A missing or `null` Origin requires checking browser/iframe restrictions. Keep the origin check enabled. The startup log includes the effective public scheme; `git pull` alone does not update a running Python process or recreate its environment.
- **`PermissionError` in `socket.socketpair()` during startup:** a container/host policy may deny the Unix socket pair used internally by Python's event loop. The bridge automatically falls back to a loopback TCP pair for internal wakeups; its temporary listener closes immediately. This requires no privileged mode, security-profile changes, or additional published port. Update the source and recreate/restart the app to pick up the fix. If loopback TCP is also denied, startup now reports an event-loop initialization failure instead of claiming that port 8081 could not bind; check the host's policy/audit logs for the denied operation.
- **WebSocket fails:** reload Nginx with the new `/spice/ws` route; verify AccessForge can reach the configured proxy on 3128 and that inter-node 3128 is allowed. Working API access on 8006 alone does not prove this.
- **VM unavailable:** check that it is running, its display supports SPICE, and the user has `VM.Audit` and `VM.Console`. Try the noVNC link.
- **TLS tunnel failure:** verify API access is reaching the intended cluster and the target node's SPICE certificate matches the returned cluster CA/subject. The bridge does not bypass TLS verification.
- **Unexpected protocol mismatch:** the client uses this message when a connection closes before the SPICE handshake finishes; it does not establish a SPICE version incompatibility. Update the bridge to include the signed routing ticket in the CONNECT `Host` header, which Proxmox requires. If the error persists, inspect app logs for `SPICE tunnel failed` or `Proxmox SPICE CONNECT rejected (HTTP ...)`. Only the HTTP status is logged, not routing tickets or response bodies.
- **Resizing does nothing:** verify the guest SPICE agent is installed and running. AccessForge does not install guest software.
- **Kali/Xfce receives a preferred size but stays at the old resolution:** check `xrandr` inside the guest. If the mode marked `+` matches the browser request while the connected output header still shows a different active size, the SPICE request reached the guest but its X11 session did not apply it. Some QXL versions omit the usual `*` active mode marker. `xrandr --output Virtual-1 --auto` applies the preferred size once (use the connected output name shown by `xrandr`). For automatic updates on this guest, copy the two `scripts/spice-xfce-follow-display.sh` and `scripts/install-spice-xfce-follow-display.sh` files into the guest, run `sh install-spice-xfce-follow-display.sh`, then log out and back in. The helper checks QXL's preferred mode once per second and applies it only when the preferred mode changes, so manual resolution changes are left alone until the next browser resize. This is an opt-in guest workaround; the browser cannot force Xfce to apply a mode after the guest ignores a SPICE monitor request.
- **After migration:** click **Re-Fit Window** to look up the VM's new node.

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
