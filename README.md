# CIT VM Accessor

Lightweight Flask app that signs into the Proxmox VE API and then launches the built‑in Proxmox noVNC console for a selected VM. The app obtains a PVE ticket and CSRF token via `/api2/json/access/ticket`, sets browser cookies for the Proxmox host, and redirects you to the console page.

## Features

- Login to Proxmox with username/password (realm configurable)
- Lists visible VMs (requires VM.Audit)
- Opens Proxmox's native noVNC console (requires VM.Console)
- Single-file app (`main.py`) served with `waitress`

## Requirements

- Python 3.8+
- A reachable Proxmox VE host (default: `https://<host>:8006`)

## Quick start

1) Create and activate a virtual environment (recommended):

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2) Install dependencies:

```bash
pip install -r requirements.txt
```

3) Configure environment variables:

```bash
export PROXMOX_HOST="pve.example.com"   # or 127.0.0.1 if running on the PVE host
export PROXMOX_REALM="pam"              # or 'pve' / 'ldap' / etc.
export VERIFY_SSL="false"               # 'true' if you have a valid cert
export FLASK_SECRET_KEY="$(python -c 'import os,base64; print(base64.b64encode(os.urandom(24)).decode())')"
export PORT=8080                         # optional, default 8080
```

4) Optional: enable HTTPS

Set paths to your certificate and key. For local testing you can create a self-signed cert:

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -keyout certs/key.pem -out certs/cert.pem -days 365 -subj "/CN=localhost"
export HTTPS_CERT_FILE="$PWD/certs/cert.pem"
export HTTPS_KEY_FILE="$PWD/certs/key.pem"
```

5) Run the app:

- Development (HTTP):

```bash
python main.py
```

- Production-ish via waitress (HTTP):

```bash
python -m waitress --listen=0.0.0.0:${PORT:-8080} main:app
```

Or use the provided runner in `main.py`:

```bash
python main.py
```

Then open http://localhost:8080 or https://localhost:8080 (if HTTPS is enabled) in your browser.

## Run with Docker Compose

1) Build and start:

```bash
docker compose up --build -d
```

2) Open the app:

- HTTP: http://localhost:8080
- For in-container HTTPS, first create certs locally and mount them (uncomment in compose):
	```bash
	mkdir -p certs
	openssl req -x509 -newkey rsa:2048 -nodes -keyout certs/key.pem -out certs/cert.pem -days 365 -subj "/CN=localhost"
	```
	Then set envs and re-up:
	```bash
	export HTTPS_CERT_FILE=/certs/cert.pem
	export HTTPS_KEY_FILE=/certs/key.pem
	docker compose up --build -d
	```

Environment variables can be set via your shell or a `.env` file in the project root. The compose file reads:

- PROXMOX_HOST (default 127.0.0.1)
- PROXMOX_REALM (default pam)
- VERIFY_SSL (default false)
- FLASK_SECRET_KEY (default change-me-now; set to a strong random string for sessions)
- LOG_LEVEL (default INFO)
- DEBUG_HTTP (default false)

To stop and clean up:

```bash
docker compose down
```

## Permissions in Proxmox

Create or use a Proxmox user with at least:

- To list VMs: `VM.Audit` on `/` (or relevant paths)
- To open console: `VM.Console` on `/vms` (or specific VMs)

## Configuration

The app reads these environment variables:

- `PROXMOX_HOST` (default: `127.0.0.1`)
- `PROXMOX_REALM` (default: `pam`)
- `VERIFY_SSL` (default: `false`) — set to `true` for valid TLS certs
- `FLASK_SECRET_KEY` — session secret; set to a strong random value
- `PORT` (default: `8080`)

## Endpoints

- `GET/POST /login` — authenticate against Proxmox and set cookies
- `GET /` — home page with simple VM list and console launcher
- `POST /open` — redirects to Proxmox noVNC console for a chosen VM
- `GET /logout` — clears session and cookies
- `GET /healthz` — basic health and config echo

## Security notes

- If serving behind HTTPS, the cookies are set with `secure` when the request is secure or `X-Forwarded-Proto: https` is present.
- Always protect this app behind TLS and restrict access appropriately.
- Do not log or store user passwords.

## Troubleshooting

- Certificate issues to `:8006`: set `VERIFY_SSL=true` only if the certificate is valid; otherwise leave `false` for testing, but prefer fixing certs.
- No VMs listed: your account likely lacks `VM.Audit` permission.
- Console fails to open: ensure `VM.Console` permission and that the cookies `PVEAuthCookie` and `CSRFPreventionToken` are present for the Proxmox host domain.

## License

Add your license here.
