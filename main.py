#!/usr/bin/env python3
# Author: Jaime Acosta
 
import os
import functools
import urllib.parse
import logging
import traceback
import uuid
import requests
import time
from flask import Flask, request, redirect, session, make_response, render_template_string, url_for, g, jsonify
from waitress import serve
from jinja2 import DictLoader

"""
Quick start
-----------
1) pip install flask waitress requests
2) Set environment variables (example):
   export PROXMOX_HOST="pve.example.com"   # or 127.0.0.1 if running on the PVE host
   export PROXMOX_REALM="pam"              # or 'pve' / 'ldap' / etc.
   export VERIFY_SSL="false"               # 'true' if you have a valid cert
   export FLASK_SECRET_KEY="$(python -c 'import os,base64; print(base64.b64encode(os.urandom(24)).decode())')"

3) Run:
   python proxmox_console_app.py
   # or production:
      <div style="display:flex; flex-direction:column; justify-content:flex-end;">
        <label style="font-size:.7rem; text-transform:uppercase; letter-spacing:.5px; font-weight:600; margin-bottom:.25rem;">Verify SSL</label>
        <label for="verifyBox" style="display:flex; align-items:center; gap:.45rem; font-size:.75rem; cursor:pointer; margin:0; font-weight:500; padding:.15rem .3rem .15rem .1rem; background:#f8fafc; border:1px solid #cfd9e3; border-radius:6px;">
          <input id="verifyBox" type="checkbox" name="verify_ssl" value="1" {% if verify_ssl %}checked{% endif %} style="transform:scale(1.05)"/>
        </label>
      </div>

How it works
------------
- User logs into this Flask app with their Proxmox username/password.
- We call /api2/json/access/ticket to get PVEAuthCookie + CSRFPreventionToken.
- We set those as cookies for the Proxmox host, so the browser can access 8006.
- We redirect the user to Proxmox’s built-in noVNC page for the VM they chose.
"""

PROXMOX_HOST = os.environ.get("PROXMOX_HOST", "127.0.0.1").strip()
PROXMOX_REALM = os.environ.get("PROXMOX_REALM", "pam").strip()
PROXMOX_PORT = os.environ.get("PROXMOX_PORT", "8006").strip()
VERIFY_SSL = os.environ.get("VERIFY_SSL", "false").lower() in ("1", "true", "yes", "y")
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-me-now")
EMBED_ALLOW = os.environ.get("EMBED_ALLOW", "true").lower() in ("1","true","yes","y")
EMBED_ALLOW_ORIGINS = os.environ.get("EMBED_ALLOW_ORIGINS", "*")  # space or comma separated
EMBED_COOKIES = os.environ.get("EMBED_COOKIES", "true").lower() in ("1","true","yes","y")

# Logging/Debug configuration
LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG").upper()
DEBUG_HTTP = os.environ.get("DEBUG_HTTP", "false").lower() in ("1", "true", "yes", "y")

def configure_logging():
  level = getattr(logging, LOG_LEVEL, logging.DEBUG)
  logging.basicConfig(
    level=level,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
  )
  # Be a bit more verbose for our app
  logging.getLogger(__name__).setLevel(level)
  # Quiet overly noisy loggers unless debugging HTTP
  if not DEBUG_HTTP:
    logging.getLogger("urllib3").setLevel(logging.WARNING)
  else:
    try:
      import http.client as http_client
      http_client.HTTPConnection.debuglevel = 1
    except Exception:
      pass
    for name in (
      "urllib3",
      "urllib3.connection",
      "urllib3.connectionpool",
      "requests.packages.urllib3",
    ):
      logging.getLogger(name).setLevel(logging.DEBUG)
      logging.getLogger(name).propagate = True
  # Waitress logs
  logging.getLogger("waitress").setLevel(logging.INFO)

configure_logging()
logger = logging.getLogger(__name__)

BASE_API = f"https://{PROXMOX_HOST}:{PROXMOX_PORT}/api2/json"

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ---------- HTML (inline templates to keep it single-file) ----------

TPL_BASE = """
<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>{{ title or "Acosta VM Accessor" }}</title>
<style>
  :root {
    --bg: #f4f7fa;
    --panel: #ffffffcc;
    --border: #cfd9e3;
    --accent: #07b36d;
    --accent-glow: 0 0 0 3px #07b36d33;
    --danger: #c62828;
    --warn: #ef6c00;
    --ok: #2e7d32;
    --text: #0e2336;
    --muted: #5c6f80;
    --mono: 'SFMono-Regular', Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  html, body { height:100%; }
  body { font-family: system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:0; padding:0 1.25rem 0; background: radial-gradient(circle at 15% 20%, #ffffff, var(--bg)); color: var(--text); min-height:100vh; display:flex; flex-direction:column; }
  .topbar { position:sticky; top:0; backdrop-filter: blur(6px); background:linear-gradient(90deg,#0d1e30,#14384f); padding:0.9rem 1rem; margin:0 -1.25rem 1rem; display:flex; justify-content:space-between; align-items:center; color:#e9f5ff; box-shadow:0 2px 6px -2px #002b4099; }
  .topbar strong { letter-spacing:.5px; font-weight:600; }
  .topbar a { color:#9feaf9; }
  a { text-decoration:none; color: var(--accent); }
  a:hover { text-decoration:underline; }
  .card { width:100%; max-width:1100px; margin:0 auto 1.2rem; border:1px solid var(--border); background:var(--panel); backdrop-filter:blur(8px); border-radius:14px; padding:1.4rem 1.5rem 1.8rem; box-shadow:0 4px 18px -6px #001d2f33; flex:1; display:flex; flex-direction:column; }
  h2,h3 { margin-top:0; font-weight:600; letter-spacing:.5px; }
  input, select, button { font: inherit; line-height:1.2; }
  input, select { width:100%; padding:.55rem .65rem; border:1px solid var(--border); border-radius:8px; background:#fff; margin-bottom:.7rem; }
  input:focus { outline:2px solid var(--accent); box-shadow: var(--accent-glow); }
  button { padding:.6rem 1.1rem; border:1px solid var(--accent); background:linear-gradient(180deg,var(--accent),#049158); color:#fff; border-radius:10px; font-weight:600; letter-spacing:.4px; display:inline-flex; gap:.35rem; align-items:center; box-shadow:0 2px 6px -2px #02603a90; }
  button:hover { filter:brightness(1.05); }
  button:active { transform:translateY(1px); }
  .btn-danger { border-color:var(--danger); background:linear-gradient(180deg,#d84343,#b02121); box-shadow:0 2px 6px -2px #5d0f0f99; }
  .btn-danger:hover { filter:brightness(1.07); }
  .row { display:flex; gap:.75rem; }
  .row > * { flex:1; }
  .error { color: var(--danger); margin-bottom:.8rem; font-weight:500; }
  .notice { background:#daf7eccc; border:1px solid #9be0c7; padding:.6rem .75rem; border-radius:8px; margin-bottom:1rem; font-size:.9rem; }
  .vm-list { display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:.65rem; margin:0 0 1rem; }
  .vm-item { position:relative; display:flex; align-items:flex-start; gap:.5rem; border:1px solid var(--border); border-radius:10px; padding:.55rem .6rem .55rem 2.2rem; background:#fff; min-height:60px; overflow:hidden; cursor:pointer; transition:border-color .18s, box-shadow .18s, background .25s; }
  .vm-item:hover { border-color:var(--accent); box-shadow:0 0 0 2px #07b36d1f, 0 4px 10px -4px #022e2044; }
  .vm-item input[type=checkbox] { position:absolute; left:.65rem; top:.75rem; width:1.05rem; height:1.05rem; margin:0; accent-color: var(--accent); cursor:pointer; }
  .vm-item a { display:flex; flex-direction:column; gap:.25rem; color:inherit; flex:1; }
  .vm-item a:hover { text-decoration:none; }
  .vm-id-line { font:600 .85rem var(--mono); letter-spacing:.5px; color:#083f2d; text-shadow:0 0 1px #07b36d44; }
  .vm-name { font-weight:600; font-size:.95rem; line-height:1.1; }
  .vm-status { font-size:.72rem; font-weight:600; letter-spacing:1px; text-transform:uppercase; display:inline-block; padding:.17rem .45rem; border-radius:20px; background:#cbd5e1; color:#24323f; box-shadow:inset 0 0 0 1px #93a6b7; }
  .vm-status.running { background:#d0f4e4; color:#055230; box-shadow:inset 0 0 0 1px #07b36d; }
  .vm-status.stopped, .vm-status.paused { background:#ffe4d5; color:#7c2b00; box-shadow:inset 0 0 0 1px #ff924d; }
  .vm-status.changed { outline:2px solid #07b36d; animation: pulse 1.1s ease-out; }
  @keyframes pulse { 0% { transform:scale(.9); filter:brightness(1.4);} 70% { transform:scale(1.03);} 100% { transform:scale(1); filter:brightness(1);} }
  .bulk-actions { display:flex; gap:.6rem; flex-wrap:wrap; }
  /* Simplified single-column layout */
  .with-side { display:block; }
  .vm-action-layout { display:grid; grid-template-columns: 1fr 210px; gap:1.25rem; align-items:start; }
  @media (max-width:1050px){ .vm-action-layout { grid-template-columns:1fr; } .action-frame { position:relative; top:auto; } }
  .action-frame { position:sticky; top:68px; display:flex; flex-direction:column; gap:.65rem; background:#0d1e30f2; backdrop-filter:blur(8px); padding:.9rem .95rem 1.1rem; border:1px solid #12384f; border-radius:14px; box-shadow:0 4px 18px -6px #001d2f66, 0 0 0 1px #12425c inset; min-height:140px; }
  .action-frame h4 { margin:0 0 .4rem; font-size:.68rem; letter-spacing:.55px; font-weight:600; text-transform:uppercase; color:#9feaf9; text-align:center; }
  .action-frame .btn-group { display:flex; flex-direction:column; gap:.45rem; }
  .action-frame button { width:100%; justify-content:center; min-height:38px; }
  .action-frame .small-group { display:flex; gap:.4rem; }
  .action-frame .small-group button { flex:1; min-height:32px; font-size:.65rem; }
  .activity-frame { margin:1.2rem 0 0; border:1px solid #12384f; background:#0d1e30f2; backdrop-filter:blur(8px); border-radius:14px; box-shadow:0 4px 18px -6px #001d2f66, 0 0 0 1px #12425c inset; padding:.4rem 0 .2rem; display:flex; flex-direction:column; }
  .activity-frame h4 { margin:.2rem .9rem .4rem; font-size:.7rem; letter-spacing:.6px; text-transform:uppercase; font-weight:600; color:#9feaf9; display:flex; justify-content:center; gap:.75rem; align-items:center; }
  .activity-frame h4 button { position:static; width:auto; }
  .activity-dock { position:relative; background:transparent; border:0; border-radius:0; box-shadow:none; width:100%; max-width:none; }
  .activity-dock { position:fixed; left:0; right:0; bottom:0; background:#0d1e30f2; color:#e2f2ff; font-size:.72rem; font-family:var(--mono); max-height:40vh; border-top:1px solid #12384f; box-shadow:0 -4px 12px -6px #000a; display:flex; flex-direction:column; backdrop-filter: blur(8px); }
  /* Reworked dock: becomes an inline frame (not fixed) and can resize */
  .activity-dock { position:relative; background:#0d1e30f2; color:#e2f2ff; font-size:.72rem; font-family:var(--mono); height:28vh; max-height:60vh; min-height:34px; border:1px solid #12384f; border-radius:10px 10px 0 0; box-shadow:0 -4px 12px -6px #000a, 0 2px 4px -2px #001923 inset; margin:0 auto; width:100%; max-width:1100px; overflow:hidden; }
  .activity-dock.collapsed { height:34px !important; min-height:34px; }
  .dock-resize-handle { position:absolute; top:0; left:0; right:0; height:6px; cursor:ns-resize; background:linear-gradient(90deg,#16455d,#102c3d); opacity:.6; }
  .activity-dock.resizing { user-select:none; }
  .activity-dock .dock-header { padding:.35rem .75rem; display:flex; justify-content:space-between; align-items:center; font-weight:600; letter-spacing:.5px; background:linear-gradient(90deg,#12384f,#0d1e30); }
  .dock-last { flex:1; font-weight:400; font-size:.65rem; color:#9cc9d9; margin:0 .65rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .activity-dock .dock-toggle { background:transparent; color:#9feaf9; border:1px solid #255b76; padding:.15rem .55rem; border-radius:6px; font-size:.65rem; line-height:1; cursor:pointer; }
  .activity-dock .dock-toggle:hover { background:#163c4f; }
  .activity-dock .dock-clear { background:transparent; color:#b3f5d9; border:1px solid #216648; padding:.15rem .55rem; border-radius:6px; font-size:.65rem; line-height:1; cursor:pointer; margin-right:.35rem; }
  .activity-dock .dock-clear:hover { background:#0f4530; }
  .activity-dock .dock-body { padding:.4rem .6rem .7rem; overflow-y:auto; overflow-x:hidden; display:flex; flex-direction:column; gap:.25rem; }
  .log-line { padding:.15rem .4rem; border-radius:4px; background:#1c3140; box-shadow:inset 0 0 0 1px #284e63; }
  .log-line.info { background:#173042; }
  .log-line.success { background:#0d3b2a; box-shadow:inset 0 0 0 1px #0e6042; }
  .log-line.warn { background:#452e09; box-shadow:inset 0 0 0 1px #6f460e; }
  .log-line.error { background:#4d1c1c; box-shadow:inset 0 0 0 1px #7e2a2a; }
  .muted { color:var(--muted); }
  code { font-family:var(--mono); font-size:.85rem; background:#ecf2f6; padding:.15rem .4rem; border-radius:6px; }
  footer { margin-top:1.5rem; text-align:center; font-size:.7rem; color:var(--muted); }
  /* dynamic bottom padding applied inline via JS to avoid overlap */
  .divider { height:1px; background:linear-gradient(90deg,transparent,#b8c9d6,transparent); margin:1.2rem 0; border-radius:2px; }
  .inline-form { display:inline; }
  .actions-row { margin-top:.4rem; }
  .vm-item:focus-within { outline:2px solid var(--accent); }
  @media (max-width:640px){ .vm-list { grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); } }
  .progress-overlay { position:fixed; inset:0; background:rgba(10,20,30,.45); display:none; align-items:center; justify-content:center; z-index:9999; }
  .progress-card { background:#0f2434; color:#e7f6ff; border:1px solid #1f4b63; border-radius:12px; padding:1.1rem 1.2rem; min-width:260px; max-width:90vw; box-shadow:0 8px 24px -10px #000a; display:flex; flex-direction:column; gap:.6rem; }
  .progress-title { font-weight:700; letter-spacing:.4px; font-size:.9rem; }
  .progress-msg { font-size:.78rem; color:#b6d7e6; }
  .progress-bar { height:6px; border-radius:6px; background:#12384f; overflow:hidden; }
  .progress-bar span { display:block; height:100%; width:40%; background:linear-gradient(90deg,#07b36d,#30d08c); animation: progress-indef 1.1s ease-in-out infinite; }
  @keyframes progress-indef { 0%{ transform:translateX(-60%);} 100%{ transform:translateX(220%);} }
</style>
</head>
<body>
  <div class=\"grid-overlay\"></div>
  <div class=\"topbar\">
  <div><strong>&#128274; Acosta VM Accessor</strong></div>
    <div>
      {% if session.get('pve_user') %}
        <span class="muted">{{ session.get('pve_user') }}</span> |
        <a href="{{ url_for('logout') }}">Logout</a>
      {% endif %}
    </div>
  </div>
  <div class=\"card\">
    {% block content %}{% endblock %}
  <!-- Footer removed per user request -->
  </div>
</body>
</html>
"""

# Register in-memory base template for Jinja to resolve `{% extends "base.html" %}`
app.jinja_loader = DictLoader({"base.html": TPL_BASE})

@app.after_request
def _allow_iframe(resp):
  if EMBED_ALLOW:
    # Normalize origins list
    origins_raw = [o.strip() for o in EMBED_ALLOW_ORIGINS.replace(',', ' ').split() if o.strip()]
    # X-Frame-Options: if specific single origin, set ALLOW-FROM (legacy); otherwise omit to rely on CSP frame-ancestors
    if 'X-Frame-Options' in resp.headers:
      del resp.headers['X-Frame-Options']
    # Construct CSP frame-ancestors directive
    if origins_raw:
      fa = ' '.join(origins_raw) if origins_raw[0] != '*' else "*"
    else:
      fa = "*"
    existing_csp = resp.headers.get('Content-Security-Policy','')
    if 'frame-ancestors' not in existing_csp:
      csp_prefix = existing_csp + ('; ' if existing_csp else '')
      resp.headers['Content-Security-Policy'] = f"{csp_prefix}frame-ancestors {fa}"
  return resp

TPL_LOGIN = """
{% extends "base.html" %}
{% block content %}
<h2>Sign in to Proxmox</h2>
{% if error %}<div class="error">{{ error }}</div>{% endif %}
<form method="post" id="loginForm">
  <label>Username</label>
  <input id="usernameInput" name="username" placeholder="e.g. root or root@pam" value="{{ username or '' }}" required />
  <label>Password</label>
  <input name="password" type="password" required />
  <details style="margin:.6rem 0 .2rem; border:1px solid #cfd9e3; padding:.6rem .75rem .75rem; border-radius:8px; background:#fff">
    <summary style="cursor:pointer; font-weight:600; outline:none">Advanced</summary>
    <div style="display:grid; gap:.55rem; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); margin-top:.65rem">
      <div>
        <label style="font-size:.7rem; text-transform:uppercase; letter-spacing:.5px; font-weight:600">Proxmox Host</label>
        <input name="host" value="{{ host }}" placeholder="pve.example.com" />
      </div>
      <div>
        <label style="font-size:.7rem; text-transform:uppercase; letter-spacing:.5px; font-weight:600">Port</label>
        <input name="port" value="{{ port }}" placeholder="443" />
      </div>
      <div>
        <label style="font-size:.7rem; text-transform:uppercase; letter-spacing:.5px; font-weight:600">Realm</label>
  <input id="realmInput" name="realm" value="{{ realm }}" placeholder="pam" />
      </div>
      <div style="display:flex; flex-direction:column; justify-content:flex-start;">
        <label style="font-size:.7rem; text-transform:uppercase; letter-spacing:.5px; font-weight:600; margin:0 0 .42rem;">Verify SSL</label>
        <div style="display:flex; align-items:center; padding:.48rem .55rem; border:1px solid #cfd9e3; border-radius:8px; background:#fff; height:38px; line-height:1;">
          <input id="verifyBox" aria-label="Verify SSL" type="checkbox" name="verify_ssl" value="1" {% if verify_ssl %}checked{% endif %} style="margin:0; width:1.05rem; height:1.05rem; cursor:pointer;"/>
          <span style="font-size:.62rem; margin-left:.55rem; color:#576b7a; font-weight:500;">Uncheck only for self‑signed certs</span>
        </div>
      </div>
    </div>
  </details>
  <button type="submit">Sign in</button>
</form>
<script>
 (function(){
   const u=document.getElementById('usernameInput');
   const r=document.getElementById('realmInput');
   if(u && r){
     function extract(){
       const val=u.value.trim();
       if(val.includes('@')){
         const parts=val.split('@');
         if(parts.length===2 && parts[0] && parts[1]){
           u.value=parts[0];
           r.value=parts[1];
         }
       }
     }
     u.addEventListener('blur', extract);
     u.addEventListener('change', extract);
     u.addEventListener('keyup', function(ev){ if(ev.key==='@' || ev.key==='Enter') extract(); });
   }
 })();
</script>
{% endblock %}
"""

TPL_SESSION_RESET = """
{% extends "base.html" %}
{% block content %}
<h2>{{ title or "Session Reset" }}</h2>
<p class="muted" style="margin:.4rem 0 1rem">{{ message }}</p>
<div class="notice" style="max-width:420px">
  You will be redirected to the sign-in page in <strong><span id="redirectCountdown">5</span> seconds</strong>.
  <br/>If nothing happens, <a href="{{ login_url }}">click here</a>.
</div>
<script>
(function(){
  var remaining = 5;
  var el = document.getElementById('redirectCountdown');
  var timer = setInterval(function(){
    remaining -= 1;
    if(el && remaining >= 0){ el.textContent = remaining; }
    if(remaining <= 0){
      clearInterval(timer);
      window.location.href = {{ login_url|tojson }};
    }
  }, 1000);
})();
</script>
{% endblock %}
"""

TPL_HOME = """
{% extends "base.html" %}
{% block content %}
<h2>Open a VM Console</h2>
<p class="muted" style="margin-top:.1rem">Select VMs below. Click a VM card to open its console in a popup. Use bulk actions for management.</p>
<div class="top-controls" style="margin:0 0 1rem; display:flex; gap:.6rem; flex-wrap:wrap; align-items:center; justify-content:flex-end;">
  <div style="display:flex; gap:.5rem; align-items:center; flex-wrap:wrap;">
    <small class="muted" id="refreshMeta" aria-live="polite">Last refresh: never</small>
    <button type="button" id="refreshBtn" title="Refresh VM statuses">Refresh Status</button>
  </div>
</div>
 {% if request.args.get('bulk') %}
 <div class="notice">
   Bulk {{ request.args.get('bulk') }}: {{ request.args.get('done','0') }} success, {{ request.args.get('failed','0') }} failed.
   {% if request.args.get('fail_list') or request.args.get('success_list') %}
     <details style="margin-top:.4rem">
       <summary style="cursor:pointer">Details</summary>
       <ul style="margin:.4rem 0 0 .8rem; padding:0; list-style:disc">
         {% if request.args.get('success_list') %}
           {% for item in request.args.get('success_list').split(';') if item %}
           <li style="color:#2e7d32">{{ item }}</li>
           {% endfor %}
         {% endif %}
         {% if request.args.get('fail_list') %}
           {% for item in request.args.get('fail_list').split(';') if item %}
           <li style="color:#c62828">{{ item }}</li>
           {% endfor %}
         {% endif %}
       </ul>
     </details>
   {% endif %}
 </div>
 {% endif %}
{% if vms %}
<div class="vm-action-layout">
  <div>
    <h3>Visible VMs</h3>
    <form method="post" action="{{ url_for('bulk_action') }}" id="bulkForm">
      <div class="vm-list" id="vmList">
      {% for vm in vms %}
        <label class="vm-item" data-node="{{ vm.get('node') }}" data-vmid="{{ vm.get('vmid') }}">
          <input type="checkbox" name="vms" value="{{ vm.get('node') }}|{{ vm.get('type') }}|{{ vm.get('vmid') }}" />
          <a href="{{ url_for('open_console') }}?node={{ vm.get('node') }}&vmid={{ vm.get('vmid') }}" target="_blank" rel="noopener" data-node="{{ vm.get('node') }}" data-vmid="{{ vm.get('vmid') }}">
            <span class="vm-id-line">#{{ vm.get('vmid') }} · {{ vm.get('node') }}</span>
            <span class="vm-name">{{ vm.get('name','') or '(no name)' }}</span>
            <span class="vm-status {{ vm.get('status') }}" id="vm-status-{{ vm.get('node') }}-{{ vm.get('vmid') }}">{{ vm.get('status') }}</span>
          </a>
        </label>
      {% endfor %}
      </div>
      <input type="hidden" name="action" value="" id="hiddenBulkAction" />
    </form>
  </div>
  <div class="action-frame" aria-label="Bulk VM Actions">
    <h4>Bulk Actions</h4>
    <div class="btn-group">
  <button id="btnStart" type="button" disabled title="Start each selected VM">Start</button>
  <button id="btnPoweroff" type="button" class="btn-danger" disabled title="Power off (stop) each selected VM">Poweroff</button>
    </div>
    <div class="small-group">
  <button id="selectAllBtn" type="button" title="Select all visible VMs">Select All</button>
      <button id="deselectAllBtn" type="button" title="Clear all selections">Clear</button>
    </div>
  </div>
</div>
{% else %}
<p class="muted">No VMs listed, or your account lacks VM.Audit permission.</p>
{% endif %}
{% if show_dock %}
<div class="activity-frame" aria-label="Activity Console Frame">
  <h4>Activity Console</h4>
<div id="activityDock" class="activity-dock collapsed" aria-label="Activity Log Panel">
  <div class="dock-resize-handle" title="Drag to resize"></div>
  <div class="dock-header">Activity Log <span id="dockLastLine" class="dock-last"></span>
    <div style="margin-left:auto; display:flex; gap:.35rem; align-items:center">
      <button type="button" id="dockClear" class="dock-clear" aria-label="Clear Activity Log">Clear</button>
      <button type="button" id="dockToggle" class="dock-toggle" aria-expanded="false" aria-controls="dockBody" aria-label="Toggle Activity">▴</button>
    </div>
  </div>
  <div id="dockBody" class="dock-body" role="log" aria-live="polite"></div>
</div>
</div>
{% endif %}
<div id="progressOverlay" class="progress-overlay" role="dialog" aria-modal="true" aria-live="polite" aria-hidden="true">
  <div class="progress-card">
    <div class="progress-title">Working…</div>
    <div id="progressMessage" class="progress-msg">Please wait.</div>
    <div class="progress-bar" aria-hidden="true"><span></span></div>
  </div>
</div>
<script>
 (function(){
   const bulkForm = document.getElementById('bulkForm');
   const refreshBtn = document.getElementById('refreshBtn');
   const refreshMeta = document.getElementById('refreshMeta');
  const dockBody = document.getElementById('dockBody');
   const dock = document.getElementById('activityDock');
   const dockToggle = document.getElementById('dockToggle');
   const dockClear = document.getElementById('dockClear');
  const progressOverlay = document.getElementById('progressOverlay');
  const progressMessage = document.getElementById('progressMessage');
  // Fixed-height dock (no resize)
  const btnStart = document.getElementById('btnStart');
  const btnPoweroff = document.getElementById('btnPoweroff');
  const hiddenAction = document.getElementById('hiddenBulkAction');
   const LOG_KEY = 'activityLogLines';
   function ts(){ return new Date().toISOString(); }
   function addLog(msg,type){
      if(!dockBody) return;
      const div=document.createElement('div');
      div.className='log-line'+(type?(' '+type):'');
      div.textContent='['+ts()+'] '+msg;
      dockBody.appendChild(div);
      dockBody.scrollTop = dockBody.scrollHeight;
  try { if(typeof console!== 'undefined' && console.debug){ console.debug('[dock]', div.textContent); } } catch(e){}
      try {
        const existing = JSON.parse(sessionStorage.getItem(LOG_KEY)||'[]');
        existing.push(div.textContent);
        if(existing.length>500) existing.splice(0, existing.length-500); // cap
        sessionStorage.setItem(LOG_KEY, JSON.stringify(existing));
      } catch(e){}
   }
   // Restore previous log entries
   try {
     const prev = JSON.parse(sessionStorage.getItem(LOG_KEY)||'[]');
     prev.forEach(line=>{ const div=document.createElement('div'); div.className='log-line'; div.textContent=line; dockBody.appendChild(div); });
     if(prev.length) dockBody.scrollTop = dockBody.scrollHeight;
   } catch(e){}
  // (Replaced by scroll-preserving toggle later after padding helper is defined)
  // Original simple toggle removed to prevent scroll jump.
  dockClear && dockClear.addEventListener('click',()=>{ if(dockBody){ dockBody.innerHTML=''; sessionStorage.removeItem(LOG_KEY); addLog('Activity log cleared','info'); }});
    // Update enabled/disabled state for central bulk buttons
    function updateBulkButtons(){
      const any = !!document.querySelector('.vm-item input[type=checkbox]:checked');
      if(btnStart) btnStart.disabled = !any;
  if(btnPoweroff) btnPoweroff.disabled = !any;
    }
  const vmCheckboxes = document.querySelectorAll('.vm-item input[type=checkbox]');
  vmCheckboxes.forEach(cb=>{ cb.addEventListener('change', updateBulkButtons); });
  updateBulkButtons();
  // Select / Deselect all controls
  const selectAllBtn = document.getElementById('selectAllBtn');
  const deselectAllBtn = document.getElementById('deselectAllBtn');
  selectAllBtn && selectAllBtn.addEventListener('click', ()=>{ vmCheckboxes.forEach(cb=>cb.checked=true); updateBulkButtons(); addLog('All VMs selected','info'); });
  deselectAllBtn && deselectAllBtn.addEventListener('click', ()=>{ vmCheckboxes.forEach(cb=>cb.checked=false); updateBulkButtons(); addLog('All VMs deselected','info'); });
   function setBusy(flag, label){ const btns=document.querySelectorAll('button'); btns.forEach(b=>{ if(flag){ if(!b.dataset.originalText){ b.dataset.originalText=b.textContent; } b.disabled=true; if(label) b.textContent=label; } else { b.disabled=false; if(b.dataset.originalText){ b.textContent=b.dataset.originalText; delete b.dataset.originalText; } } }); }
   function showProgress(msg){
     if(progressOverlay){
       if(progressMessage){ progressMessage.textContent = msg || 'Please wait.'; }
       progressOverlay.style.display='flex';
       progressOverlay.setAttribute('aria-hidden','false');
     }
   }
   function hideProgress(){
     if(progressOverlay){
       progressOverlay.style.display='none';
       progressOverlay.setAttribute('aria-hidden','true');
     }
   }
  if(bulkForm){ bulkForm.addEventListener('submit', function(ev){
      const selected=[...document.querySelectorAll('.vm-item input[type=checkbox]:checked')].map(cb=>cb.value);
      // Determine action from submitter OR hidden action field (for floating panel submission)
      const hiddenActionInput = bulkForm.querySelector('input[name=action]');
      const actionBtn = (ev && ev.submitter && ev.submitter.value) || (document.activeElement && document.activeElement.value) || (hiddenActionInput && hiddenActionInput.value) || '(unknown)';
      if(!selected.length){ ev.preventDefault(); addLog('No VMs selected; action aborted','warn'); return; }
      // Confirmation dialog before submitting
  const previewList = selected.slice(0,15).map(v=>v.split('|')[2]).join(', ')+(selected.length>15?' ...':'');
  // Use an escaped \\n for readability in the confirm dialog
  const confirmMsg = 'Proceed with '+actionBtn.toUpperCase()+' on '+selected.length+' VM(s)?\\nVMIDs: '+previewList; 
      if(!window.confirm(confirmMsg)){
        ev.preventDefault();
        addLog('Bulk '+actionBtn+' canceled by user','warn');
        // clear hidden action so future attempts can set it again
        if(hiddenActionInput) hiddenActionInput.value='';
        hideProgress();
        return;
      }
      addLog('DEBUG bulk submit (pre) action_btn='+actionBtn+' total_selected='+selected.length+' values=['+selected.join(',')+'] formAction='+bulkForm.getAttribute('action'),'info');
      showProgress('Submitting '+actionBtn+' for '+selected.length+' VM(s)…');
  // Disable buttons but keep their labels unchanged
  setTimeout(()=>{ setBusy(true); addLog('Bulk action submitted (deferred disable)','info'); }, 25);
    }); }
   async function doRefresh(){
     if(!refreshBtn) return;
  setBusy(true);
  showProgress('Refreshing VM status…');
     try {
       const r = await fetch('{{ url_for('api_vms') }}',{headers:{'Accept':'application/json'}});
       if(r.status === 401){
         let redirectTarget = '{{ url_for('session_reset', reason='invalid') }}';
         try {
           const data = await r.json();
           if(data && data.redirect){ redirectTarget = data.redirect; }
         } catch(ignore){}
         addLog('Session expired; redirecting to sign-in','warn');
         if(refreshMeta){ refreshMeta.textContent='Session expired; redirecting…'; }
         setTimeout(()=>{ window.location.href = redirectTarget; }, 250);
         return;
       }
  if(!r.ok) throw new Error('HTTP '+r.status);
  const data = await r.json();
  let updated=0;
       (data.vms||[]).forEach(vm=>{
         const id='vm-status-'+vm.node+'-'+vm.vmid;
         const el=document.getElementById(id);
         if(el){
           const old=el.textContent;
           if(old!==vm.status){
             el.textContent=vm.status;
             el.className='vm-status '+vm.status+' changed';
             setTimeout(()=>{ el.classList.remove('changed'); },1200);
           }
           updated++;
         }
       });
       if(refreshMeta){
         const stamp = (new Date()).toLocaleTimeString();
         const label = updated === 1 ? 'status' : 'statuses';
         refreshMeta.textContent='Last refresh: '+stamp+' • '+updated+' '+label+' updated';
       }
       addLog('Refresh completed ('+updated+' statuses)','info');
     } catch(e){
  addLog('Refresh failed: '+e.message,'error');
  if(refreshMeta){ refreshMeta.textContent='Last refresh failed'; }
     } finally {
       setBusy(false);
       hideProgress();
     }
   }
   if(refreshBtn){ refreshBtn.addEventListener('click', doRefresh); }
  const lastAction = {{ last_action|tojson }}; if(lastAction && lastAction.action){ addLog('Bulk '+lastAction.action+' summary: '+(lastAction.done||0)+' ok, '+(lastAction.failed||0)+' failed'+(lastAction.skipped?(', '+lastAction.skipped+' skipped'):'') , (parseInt(lastAction.failed||0)>0)?'warn':'success'); }
  const params = new URLSearchParams(window.location.search);
  const failListRaw = params.get('fail_list');
  const successListRaw = params.get('success_list');
  const skipListRaw = params.get('skip_list');
  if(successListRaw){ successListRaw.split(';').forEach(s=>{ if(s.trim()) addLog('✔ '+s.trim(),'success'); }); }
  if(skipListRaw){ skipListRaw.split(';').forEach(s=>{ if(s.trim()) addLog('↷ '+s.trim(),'info'); }); }
  if(failListRaw){ failListRaw.split(';').forEach(f=>{ if(f.trim()) addLog('✖ '+f.trim(),'error'); }); }
  // Auto-refresh disabled per user request.

  // Intercept VM card link clicks to open popup window instead of a new tab
  const vmLinks = document.querySelectorAll('.vm-list .vm-item a');
  vmLinks.forEach(a=>{
    a.addEventListener('click', function(ev){
      // Only intercept simple left click (no modifiers). Otherwise let browser handle (incl. Ctrl/Cmd+click new tab).
      if(ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
      ev.preventDefault();
      const url = this.href;
      const vmid = this.getAttribute('data-vmid') || 'vm';
      const features = 'width=1100,height=760,menubar=no,toolbar=no,location=no,status=no,resizable=yes,scrollbars=yes';
      let win = null;
      try { win = window.open(url, 'vm_console_'+vmid, features); } catch(e) { /* ignore */ }
      if(!win){
        // Try a plain new tab
        try { win = window.open(url, '_blank'); } catch(e) { /* ignore */ }
      }
      if(!win){
        addLog('Popup blocked; falling back to same-tab navigation','warn');
        window.location.href = url;
        return;
      }
      try { win.focus(); } catch(e){}
      addLog('Opened console '+(win===window?'(same tab) ':'')+'for VM '+vmid,'info');
    });
  });

  // Reworked dock: inline frame with resize + collapse
  if(dock){
    const resizeHandle = dock.querySelector('.dock-resize-handle');
    let isResizing=false; let startY=0; let startH=0;
    function applyHeight(h){
      const min=34; const max = Math.min(window.innerHeight*0.75, 600);
      h=Math.max(min, Math.min(max, h));
      dock.style.height = h+"px";
    }
    if(resizeHandle){
      resizeHandle.addEventListener('mousedown', (e)=>{
        if(dock.classList.contains('collapsed')) return;
        isResizing=true; startY=e.clientY; startH=dock.getBoundingClientRect().height; dock.classList.add('resizing');
        e.preventDefault();
      });
      window.addEventListener('mousemove', (e)=>{ if(!isResizing) return; const delta = startY - e.clientY; applyHeight(startH + delta); });
      window.addEventListener('mouseup', ()=>{ if(isResizing){ isResizing=false; dock.classList.remove('resizing'); }});
    }
    if(dockToggle){
      dockToggle.addEventListener('click', ()=>{
        const collapsed = dock.classList.toggle('collapsed');
        dockToggle.textContent = collapsed ? '▴' : '▾';
        dockToggle.setAttribute('aria-expanded', String(!collapsed));
        if(!collapsed){
          // Expand to previous or default height
          if(!dock.style.height || parseInt(dock.style.height,10) < 120){
            applyHeight(Math.round(window.innerHeight * 0.28));
          }
          if(dockBody){ dockBody.scrollTop = dockBody.scrollHeight; }
        }
      });
    }
  }
  // Bulk action triggers (outside dock conditional so they work even if dock hidden)
  updateBulkButtons();
  function triggerAction(action){
    if(!bulkForm) return;
    hiddenAction.value = action;
    showProgress('Submitting '+action+' request…');
    let canceled = false;
    const preValue = hiddenAction.value;
    const evt = new Event('submit', {cancelable:true});
    if(!bulkForm.dispatchEvent(evt)) canceled = true; // if any listener called preventDefault via legacy path
    // If listener prevented default, canceled stays true (bulkForm listener uses preventDefault on cancel)
    if(hiddenAction.value != preValue) canceled = true; // listener cleared hidden action when canceled
    if(!canceled){
      try { bulkForm.submit(); } catch(e){ addLog('Submit error: '+e.message,'error'); }
    } else {
      hideProgress();
    }
  }
  btnStart && btnStart.addEventListener('click', ()=>triggerAction('start'));
  btnPoweroff && btnPoweroff.addEventListener('click', ()=>triggerAction('poweroff'));
 })();
</script>
{% endblock %}
"""

# ---------- Helpers ----------

def _mask(value: str, keep_end: int = 4):
  if not value:
    return value
  return ("*" * max(0, len(value) - keep_end)) + value[-keep_end:]

def _sanitize_headers(h):
  if not h:
    return {}
  masked = dict(h)
  for k in list(masked.keys()):
    lk = k.lower()
    if lk in ("authorization", "cookie", "set-cookie"):
      masked[k] = "<redacted>"
  return masked

def _sanitize_form(d):
  if not d:
    return {}
  out = {}
  for k, v in d.items():
    if k.lower() in ("password", "passwd"):
      out[k] = _mask(v)
    else:
      out[k] = v
  return out

# ---- Helper functions restored ----
def req_id():
  return getattr(g, "request_id", "-")

def cookie_host():
  return session.get("pve_host", PROXMOX_HOST)

def proxmox_request(method: str, path: str, **kwargs):
  h = session.get("pve_host", PROXMOX_HOST)
  p = session.get("pve_port", PROXMOX_PORT)
  base = f"https://{h}:{p}/api2/json"
  url = base + path if not path.startswith("http") else path
  headers = kwargs.get("headers") or {}
  form = kwargs.get("data") or kwargs.get("json") or {}
  logger.info(
    f"[{req_id()}] OUTBOUND {method.upper()} {url} params={_sanitize_form(kwargs.get('params'))} form={_sanitize_form(form)} headers={_sanitize_headers(headers)} verify={session.get('pve_verify_ssl', VERIFY_SSL)}"
  )
  verify_flag = session.get("pve_verify_ssl")
  if verify_flag is None:
    verify_flag = VERIFY_SSL
  start_time = time.time()
  resp = requests.request(method.upper(), url, verify=verify_flag, **kwargs)
  elapsed = (time.time() - start_time) * 1000.0
  preview = resp.text[:160].replace('\n',' ').replace('\r',' ')
  logger.info(
    f"[{req_id()}] INBOUND {method.upper()} {url} status={resp.status_code} elapsed_ms={elapsed:.1f} body_preview={preview!r}"
  )
  return resp

def proxmox_get(path, **kwargs):
  return proxmox_request("GET", path, **kwargs)

def proxmox_post(path, **kwargs):
  return proxmox_request("POST", path, **kwargs)

@app.before_request
def assign_request_id():
  g.request_id = uuid.uuid4().hex[:8]

# ---------- Routes ----------

def require_session(api: bool = False):
  """Decorator to ensure an active (and not soft-expired) Proxmox session.
  If api=True, returns JSON errors; otherwise redirects to login (with force on soft expiry).
  Soft expiry set to 110 minutes to preempt default Proxmox ticket timeout (~120m)."""
  def deco(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
      ticket = session.get("pve_ticket")
      if not ticket:
        if api:
          return jsonify({"error": "unauthorized"}), 401
        session.clear()
        return redirect(url_for("session_reset", reason="missing"))
      issued = session.get("pve_login_time")
      if issued and (time.time() - issued) > (110*60):
        if api:
          return jsonify({"error": "expired"}), 401
        session.clear()
        return redirect(url_for("session_reset", reason="expired"))
      return fn(*args, **kwargs)
    return wrapper
  return deco

@app.route("/login", methods=["GET", "POST"])
def login():
  if request.method == "GET":
    force = request.args.get("force") == "1"
    if force:
      # Clear session & cookies to avoid stale ticket collisions
      old_domain = session.get("pve_host") or PROXMOX_HOST
      session.clear()
      resp = make_response(render_template_string(
        TPL_LOGIN,
        host=PROXMOX_HOST,
        realm=PROXMOX_REALM,
        api=BASE_API,
        notice="Session reset. Please log in again.",
      ))
      for cname in ("PVEAuthCookie", "CSRFPreventionToken"):
        resp.set_cookie(cname, "", path="/", expires=0)
        resp.set_cookie(cname, "", domain=old_domain, path="/", expires=0)
      return resp
    return render_template_string(
      TPL_LOGIN,
      host=session.get("pve_host", PROXMOX_HOST),
      port=session.get("pve_port", PROXMOX_PORT),
      realm=session.get("pve_realm", PROXMOX_REALM),
      verify_ssl=session.get("pve_verify_ssl", VERIFY_SSL),
      username="",
      error=None,
    )

  username_input = (request.form.get("username") or "").strip()
  # Allow user to supply realm inline as user@realm; if present, split and override realm
  if "@" in username_input and not username_input.startswith("@"):  # basic guard
    parts = username_input.split("@", 1)
    username = parts[0].strip()
    inline_realm = parts[1].strip()
  else:
    username = username_input
    inline_realm = None
  password = request.form.get("password") or ""
  host_override = (request.form.get("host") or "").strip() or PROXMOX_HOST
  # Use provided port or default to configured PROXMOX_PORT (do not auto-switch 8006->443)
  port_override = (request.form.get("port") or "").strip() or PROXMOX_PORT
  realm_field = (request.form.get("realm") or "").strip() or PROXMOX_REALM
  # If inline realm specified in username, it takes precedence
  realm_override = inline_realm or realm_field
  verify_override = request.form.get("verify_ssl") == "1"
  session["pve_host"] = host_override
  session["pve_port"] = port_override
  session["pve_realm"] = realm_override
  session["pve_verify_ssl"] = verify_override

  if not username or not password:
    return render_template_string(
      TPL_LOGIN,
      host=PROXMOX_HOST,
      realm=PROXMOX_REALM,
      api=BASE_API,
      error="Username and password are required.",
    )

  # Compose full user with realm
  user_with_realm = f"{username}@{realm_override}"
  try:
    logger.debug(f"[{req_id()}] Attempting login for user={username!r} realm={PROXMOX_REALM}")
    r = proxmox_post("/access/ticket", data={"username": user_with_realm, "password": password})
    logger.debug(f"[{req_id()}] /access/ticket status={r.status_code}")
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data", {})
    ticket = data.get("ticket")
    csrf = data.get("CSRFPreventionToken")
    if not ticket or not csrf:
      raise ValueError("Missing ticket or CSRF token in response.")
    # Persist minimal session state
    session["pve_user"] = user_with_realm
    session["pve_ticket"] = ticket
    session["pve_csrf"] = csrf
    session["pve_login_time"] = time.time()
    session["pve_host"] = host_override
    session["pve_port"] = port_override
    resp = make_response(redirect(url_for("home")))
    # Set cookies so the browser will send them back to our proxy paths on the same origin (host-only cookies).
    # Also, optionally set domain cookies for the Proxmox host to support direct access scenarios.
    proxmox_domain = cookie_host()
    forwarded_host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
    app_host = forwarded_host.split(":")[0]

    secure_flag = request.is_secure or (request.headers.get("X-Forwarded-Proto", "").lower() == "https")
    same_site_mode = "None" if EMBED_COOKIES else "Lax"
    secure_effective = True if same_site_mode == "None" else secure_flag

    # Purge old cookies (host + domain) to prevent stale ordering issues
    for cname in ("PVEAuthCookie", "CSRFPreventionToken"):
      resp.set_cookie(cname, "", path="/", expires=0)
      if proxmox_domain and proxmox_domain != app_host:
        resp.set_cookie(cname, "", domain=proxmox_domain, path="/", expires=0)

    # 1) Host-only cookies for the current app origin (no domain parameter)
    resp.set_cookie(
      "PVEAuthCookie",
      ticket,
      path="/",
      httponly=True,
      samesite=same_site_mode,
      secure=secure_effective,
    )
    resp.set_cookie(
      "CSRFPreventionToken",
      csrf,
      path="/",
      samesite=same_site_mode,
      secure=secure_effective,
    )

    # 2) Additionally set cookies scoped to the Proxmox host domain (if different), for flexibility
    if proxmox_domain and proxmox_domain != app_host:
      resp.set_cookie(
        "PVEAuthCookie",
        ticket,
        domain=proxmox_domain,
        path="/",
        httponly=True,
        samesite=same_site_mode,
        secure=secure_effective,
      )
      resp.set_cookie(
        "CSRFPreventionToken",
        csrf,
        domain=proxmox_domain,
        path="/",
        samesite=same_site_mode,
        secure=secure_effective,
      )
    logger.info(f"[{req_id()}] Login successful for {user_with_realm}")
    # Post-login validation: ensure ticket actually works
    try:
      vr = proxmox_get("/version", cookies={"PVEAuthCookie": ticket}, headers={"CSRFPreventionToken": csrf})
      if not vr.ok:
        logger.warning(f"[{req_id()}] Post-login validation failed status={vr.status_code}")
        session.clear()
        for cname in ("PVEAuthCookie", "CSRFPreventionToken"):
          resp.set_cookie(cname, "", path="/", expires=0)
        return render_template_string(
          TPL_LOGIN,
          host=host_override,
          port=port_override,
          realm=realm_override,
          verify_ssl=verify_override,
          username=username,
          error=f"Ticket validation failed (HTTP {vr.status_code}). Please retry.",
        )
    except Exception:
      logger.exception(f"[{req_id()}] Post-login validation exception")
      session.clear()
      for cname in ("PVEAuthCookie", "CSRFPreventionToken"):
        resp.set_cookie(cname, "", path="/", expires=0)
      return render_template_string(
        TPL_LOGIN,
        host=host_override,
        port=port_override,
        realm=realm_override,
        verify_ssl=verify_override,
        username=username,
        error="Ticket validation exception; please login again.",
      )
    return resp
  except Exception as e:
    logger.exception(f"[{req_id()}] Login failed for {user_with_realm}")
    return render_template_string(
      TPL_LOGIN,
      host=host_override,
      port=port_override,
      realm=realm_override,
      verify_ssl=verify_override,
      username=username,
      error=f"Login failed (Request ID: {req_id()}): {e}",
    )

@app.route("/logout")
def logout():
    session.clear()
    resp = make_response(redirect(url_for("login")))
    proxmox_domain = cookie_host()
    for cname in ("PVEAuthCookie", "CSRFPreventionToken"):
        # Clear host-only cookie
        resp.set_cookie(cname, "", path="/", expires=0)
        # Clear domain cookie (if any)
        if proxmox_domain:
            resp.set_cookie(cname, "", domain=proxmox_domain, path="/", expires=0)
    return resp

@app.route("/session-reset")
def session_reset():
    reason = request.args.get("reason", "expired")
    title_map = {
      "missing": "Session Required",
      "expired": "Session Expired",
      "invalid": "Session Invalid",
    }
    message_map = {
      "missing": "We couldn't find an active session. Please sign in again to continue.",
      "expired": "Your login session has timed out. Please sign in again to continue.",
      "invalid": "Your Proxmox token is no longer valid. Please sign in again to continue.",
    }
    login_url = url_for("login", force=1)
    proxmox_domain = cookie_host()
    session.clear()
    resp = make_response(render_template_string(
      TPL_SESSION_RESET,
      title=title_map.get(reason, "Session Reset"),
      message=message_map.get(reason, "Please sign in again."),
      login_url=login_url,
    ))
    for cname in ("PVEAuthCookie", "CSRFPreventionToken"):
      resp.set_cookie(cname, "", path="/", expires=0)
      if proxmox_domain:
        resp.set_cookie(cname, "", domain=proxmox_domain, path="/", expires=0)
    return resp

@app.route("/")
@require_session()
def home():

  # Show user's visible non-template VMs
  vms = []
  try:
    cookies = {"PVEAuthCookie": session.get("pve_ticket")}
    headers = {"CSRFPreventionToken": session.get("pve_csrf")}
    r = proxmox_get(
      "/cluster/resources",
      params={"type": "vm"},
      cookies=cookies,
      headers=headers,
    )
    if r.status_code == 401:
      logger.info(f"[{req_id()}] Upstream 401 listing VMs; forcing session reset")
      session.clear()
      return redirect(url_for("session_reset", reason="invalid"))
    if r.ok:
      vms = [row for row in r.json().get("data", []) if row.get("type") in ("qemu", "lxc") and not row.get("template")]
    else:
      logger.warning(
        f"[{req_id()}] Failed to list VMs: status={r.status_code} body={r.text[:300]!r}"
      )
  except Exception:
    logger.exception(f"[{req_id()}] Exception while listing VMs")

  # Provide last action result to JS dock
  last_action = {
    "action": request.args.get("bulk"),
    "done": request.args.get("done"),
    "failed": request.args.get("failed"),
    "skipped": request.args.get("skipped"),
  }

  # Build bulk notice (for legacy notice region if any)
  bulk = request.args.get("bulk")
  done = request.args.get("done")
  failed = request.args.get("failed")
  skipped = request.args.get("skipped")
  fail_list = request.args.get("fail_list")
  success_list = request.args.get("success_list")
  skip_list = request.args.get("skip_list")
  notice = None
  if bulk:
    parts = [f"Bulk {bulk} complete: {done} ok"]
    if skipped and skipped != "0":
      parts.append(f"{skipped} skipped")
    if failed and failed != "0":
      parts.append(f"{failed} failed")
    notice = ", ".join(parts)
    if skip_list:
      notice += f" | Skipped: {skip_list}"
    if fail_list:
      notice += f" | Failures: {fail_list}"
    if success_list:
      notice += f" | Successes: {success_list}"

  return render_template_string(
    TPL_HOME,
    vms=vms,
    last_action=last_action,
    show_dock=True,
    bulk_notice=notice,
  )

@app.route("/open", methods=["GET", "POST"])
@require_session()
def open_console():

  if request.method == "POST":
    node = (request.form.get("node") or "").strip()
    vmid = (request.form.get("vmid") or "").strip()
  else:
    node = (request.args.get("node") or "").strip()
    vmid = (request.args.get("vmid") or "").strip()

  if not node or not vmid.isdigit():
    return redirect(url_for("home"))

  # Route the console through our nginx proxy on this same origin using /proxmox/
  # This avoids the browser needing to reach host.docker.internal or non-443 ports.
  qs = urllib.parse.urlencode({
    "console": "kvm",
    "novnc": "1",
    "node": node,
    "vmid": vmid,
    "resize": "scale",
  })
  console_url = f"/proxmox/?{qs}"
  logger.info(f"[{req_id()}] Redirecting to console via proxy vmid={vmid} node={node} -> {console_url}")
  return redirect(console_url, code=302)

@app.route("/bulk", methods=["POST"])
@require_session()
def bulk_action():
  action = (request.form.get("action") or "").lower().strip()
  selections = request.form.getlist("vms")
  if not action or not selections:
    return redirect(url_for("home"))
  done = 0
  failed = 0
  skipped = 0
  failure_details = []  # collect strings "node/vmid action failed (reason)"
  success_details = []  # collect strings "node/vmid action ok"
  skip_details = []     # collect strings "node/vmid skipped (reason)"
  cookies = {"PVEAuthCookie": session.get("pve_ticket")}
  headers = {"CSRFPreventionToken": session.get("pve_csrf")}
  # Fetch current statuses to allow intelligent skipping
  status_map = {}
  try:
    rs = proxmox_get(
      "/cluster/resources",
      params={"type": "vm"},
      cookies=cookies,
      headers=headers,
    )
    if rs.status_code == 401:
      logger.info(f"[{req_id()}] Upstream 401 while preparing bulk action; forcing session reset")
      session.clear()
      return redirect(url_for("session_reset", reason="invalid"))
    if rs.ok:
      for row in rs.json().get("data", []):
        status_map[(row.get("node"), str(row.get("vmid")))] = row.get("status")
  except Exception:
    logger.warning(f"[{req_id()}] Could not prefetch VM statuses for skip logic")
  for item in selections:
    try:
      node, vtype, vmid = item.split("|")
      current_status = status_map.get((node, vmid))
      logger.info(f"[{req_id()}] Bulk item action={action} node={node} vmid={vmid} type={vtype} current_status={current_status}")
      if action in ("poweroff", "reboot"):
        # Only attempt stop if currently running. Note: QEMU 'stop' is immediate poweroff; use 'shutdown' for graceful ACPI.
        if current_status and current_status != "running":
          skipped += 1
          skip_details.append(f"{node}/{vmid} skipped (not running)")
          continue
        if vtype == "qemu":
          path = f"/nodes/{node}/qemu/{vmid}/status/stop"
        elif vtype == "lxc":
          path = f"/nodes/{node}/lxc/{vmid}/status/stop"
        else:
          logger.warning(f"[{req_id()}] Unsupported VM type for poweroff: {vtype} ({item})")
          failed += 1
          continue
        logger.info(f"[{req_id()}] Sending poweroff request path={path}")
        r = proxmox_post(path, data={}, cookies=cookies, headers=headers)
        if r.status_code == 401:
          logger.info(f"[{req_id()}] Poweroff unauthorized vmid={vmid} node={node}; forcing session reset")
          session.clear()
          return redirect(url_for("session_reset", reason="invalid"))
        if r.ok:
          done += 1
          success_details.append(f"{node}/{vmid} poweroff ok")
        else:
          failed += 1
          reason = f"HTTP {r.status_code}"
          failure_details.append(f"{node}/{vmid} poweroff failed ({reason})")
          logger.warning(f"[{req_id()}] Poweroff failed vmid={vmid} node={node} status={r.status_code} body={r.text[:180]!r}")
      elif action == "start":
        if current_status and current_status == "running":
          skipped += 1
          skip_details.append(f"{node}/{vmid} skipped (already running)")
          continue
        if vtype == "qemu":
          path = f"/nodes/{node}/qemu/{vmid}/status/start"
        elif vtype == "lxc":
          path = f"/nodes/{node}/lxc/{vmid}/status/start"
        else:
          logger.warning(f"[{req_id()}] Unsupported VM type for start: {vtype} ({item})")
          failed += 1
          continue
        logger.info(f"[{req_id()}] Sending start request path={path}")
        r = proxmox_post(path, data={}, cookies=cookies, headers=headers)
        if r.status_code == 401:
          logger.info(f"[{req_id()}] Start unauthorized vmid={vmid} node={node}; forcing session reset")
          session.clear()
          return redirect(url_for("session_reset", reason="invalid"))
        if r.ok:
          done += 1
          success_details.append(f"{node}/{vmid} start ok")
        else:
          failed += 1
          reason = f"HTTP {r.status_code}"
          failure_details.append(f"{node}/{vmid} start failed ({reason})")
          logger.warning(f"[{req_id()}] Start failed vmid={vmid} node={node} status={r.status_code} body={r.text[:180]!r}")
      else:
        logger.warning(f"[{req_id()}] Unsupported bulk action: {action}")
        failed += 1
    except Exception:
      failed += 1
      failure_details.append(f"{item} exception")
      logger.exception(f"[{req_id()}] Bulk action exception processing {item}")
  fail_list = ";".join(failure_details) if failure_details else None
  success_list = ";".join(success_details) if success_details else None
  skip_list = ";".join(skip_details) if skip_details else None
  return redirect(url_for("home", bulk=action, done=done, failed=failed, skipped=skipped, fail_list=fail_list, success_list=success_list, skip_list=skip_list))

# Lightweight API endpoint returning current non-template VM statuses (used by JS refresh)
@app.route("/api/vms", methods=["GET"])
@require_session(api=True)
def api_vms():
  cookies = {"PVEAuthCookie": session.get("pve_ticket")}
  headers = {"CSRFPreventionToken": session.get("pve_csrf")}
  try:
    r = proxmox_get(
      "/cluster/resources",
      params={"type": "vm"},
      cookies=cookies,
      headers=headers,
    )
    if r.status_code == 401:
      session.clear()
      return jsonify({"error": "unauthorized", "redirect": url_for("session_reset", reason="invalid")}), 401
    if not r.ok:
      return jsonify({"error": "upstream", "status": r.status_code}), 502
    data = [row for row in r.json().get("data", []) if row.get("type") in ("qemu", "lxc") and not row.get("template")]
    # Only fields needed by UI
    slim = [
      {
        "node": row.get("node"),
        "vmid": row.get("vmid"),
        "status": row.get("status"),
        "name": row.get("name"),
        "type": row.get("type"),
      }
      for row in data
    ]
    return jsonify({"vms": slim})
  except Exception:
    logger.exception(f"[{req_id()}] /api/vms exception")
    return jsonify({"error": "exception"}), 500

# ---------- App runner ----------

@app.route("/healthz")
def healthz():
  return {"ok": True, "host": PROXMOX_HOST, "realm": PROXMOX_REALM, "verify_ssl": VERIFY_SSL}

def run():
  port = int(os.environ.get("PORT", "8080"))
  https_cert = os.environ.get("HTTPS_CERT_FILE")
  https_key = os.environ.get("HTTPS_KEY_FILE")
  if https_cert or https_key:
    logger.warning("HTTPS_CERT_FILE/HTTPS_KEY_FILE provided but waitress does not terminate TLS. Deploy behind a reverse proxy (e.g. nginx) for HTTPS.")
  logger.info(
    f"Starting waitress on http://0.0.0.0:{port} (Proxmox host: {PROXMOX_HOST}, realm: {PROXMOX_REALM}, verify_ssl={VERIFY_SSL}, log_level={LOG_LEVEL}, debug_http={DEBUG_HTTP})"
  )
  serve(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
  run()
