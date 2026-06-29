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
from flask import Flask, request, redirect, session, make_response, render_template_string, url_for, g, jsonify, send_from_directory
from waitress import serve
from jinja2 import DictLoader
import concurrent.futures
import re
import html
import json

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
- We redirect the user to Proxmox's built-in noVNC page for the VM they chose.
"""

PROXMOX_HOST = os.environ.get("PROXMOX_HOST", "127.0.0.1").strip()
PROXMOX_REALM = os.environ.get("PROXMOX_REALM", "pam").strip()
PROXMOX_PORT = os.environ.get("PROXMOX_PORT", "8006").strip()
VERIFY_SSL = os.environ.get("VERIFY_SSL", "false").lower() in ("1", "true", "yes", "y")
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-me-now")
EMBED_ALLOW = os.environ.get("EMBED_ALLOW", "true").lower() in ("1","true","yes","y")
EMBED_ALLOW_ORIGINS = os.environ.get("EMBED_ALLOW_ORIGINS", "*")  # space or comma separated
EMBED_COOKIES = os.environ.get("EMBED_COOKIES", "true").lower() in ("1","true","yes","y")
APP_DEFAULT_THEME = os.environ.get("DEFAULT_THEME", "pokemon").strip()

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

# Explicit static route to ensure correct MIME type under reverse proxy
@app.route("/static/<path:filename>")
def static_files(filename):
  if filename.endswith(".js"):
    return send_from_directory("static", filename, mimetype="text/javascript")
  return send_from_directory("static", filename)

# ---------- HTML (inline templates to keep it single-file) ----------

TPL_BASE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{{ title or "AccessForge" }}</title>
<script>
  (function() {
    var storedTheme = localStorage.getItem('accessforge-theme') || '__DEFAULT_THEME__';
    document.documentElement.setAttribute('data-theme', storedTheme);
  })();
  function changeTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('accessforge-theme', theme);
  }
</script>
<style>
  :root, [data-theme="hacker"] {
    --bg: #030a03;
    --panel: #071507ea;
    --border: #00ff41;
    --accent: #00ff41;
    --accent-glow: 0 0 10px #00ff41;
    --danger: #ff003c;
    --warn: #ffb000;
    --ok: #00ff41;
    --text: #00ff41;
    --muted: #008f11;
    --mono: 'SFMono-Regular', Menlo, Consolas, monospace;
    
    --topbar-bg: #020502dd;
    --shadow-base: #00ff4133;
    --shadow-dim: #00ff4122;
    --input-bg: #000;
    --input-focus: #051105;
    --btn-bg: #001100;
    --btn-hover: #002b0e;
    --danger-bg: #1a0000;
    --danger-hover: #330000;
    --danger-border-hover: #ff4d4d;
    --notice-bg: #051a05cc;
    --item-bg: #000;
    --item-hover: #021102;
    --scenario-bg: #000;
    --scenario-shadow: #00ff4111;
    --count-bg: #002200;
    --info-btn-bg: #000;
    --info-btn-hover: #002200;
    --info-btn-focus: #001100;
    --info-btn-shadow: #002200;
    --pop-bg: #000e00;
    --pop-shadow: #00ff4144;
    --status-bg: #002200;
    --status-run: #001a00;
    --status-stop: #1a0000;
    --action-bg: #000d00f2;
    --dock-bg: #000500f2;
    --dock-hdr: #001100;
    --dock-hov: #003300;
    --log-bg: #000a00;
    --log-succ: #001100;
    --log-warn: #1a1100;
    --log-err: #1a0000;
    --prog-over: rgba(0,10,0,.8);
    --prog-card: #000;
    --prog-bar-bg: #002200;
    --btn-sec: #001100;
    --btn-sec-hov: #002200;
  }
  [data-theme="original"] {
    --bg: #f4f5f7;
    --panel: #ffffff;
    --border: #d1d5db;
    --accent: #2563eb;
    --accent-glow: 0 0 0 transparent;
    --danger: #ef4444;
    --warn: #f59e0b;
    --ok: #10b981;
    --text: #1f2937;
    --muted: #6b7280;
    --mono: 'SFMono-Regular', Menlo, Consolas, monospace;
    
    --topbar-bg: #ffffffdd;
    --shadow-base: rgba(0,0,0,0.1);
    --shadow-dim: rgba(0,0,0,0.05);
    --input-bg: #ffffff;
    --input-focus: #f9fafb;
    --btn-bg: #f3f4f6;
    --btn-hover: #e5e7eb;
    --danger-bg: #fef2f2;
    --danger-hover: #fee2e2;
    --danger-border-hover: #ef4444;
    --notice-bg: #eff6ff;
    --item-bg: #ffffff;
    --item-hover: #f9fafb;
    --scenario-bg: #ffffff;
    --scenario-shadow: rgba(0,0,0,0.02);
    --count-bg: #e5e7eb;
    --info-btn-bg: #f9fafb;
    --info-btn-hover: #f3f4f6;
    --info-btn-focus: #e5e7eb;
    --info-btn-shadow: #e5e7eb;
    --pop-bg: #ffffff;
    --pop-shadow: rgba(0,0,0,0.15);
    --status-bg: #f3f4f6;
    --status-run: #d1fae5;
    --status-stop: #fee2e2;
    --action-bg: #ffffffdd;
    --dock-bg: #f9fafb;
    --dock-hdr: #e5e7eb;
    --dock-hov: #d1d5db;
    --log-bg: #ffffff;
    --log-succ: #d1fae5;
    --log-warn: #fef3c7;
    --log-err: #fee2e2;
    --prog-over: rgba(255,255,255,.8);
    --prog-card: #ffffff;
    --prog-bar-bg: #e5e7eb;
    --btn-sec: #f3f4f6;
    --btn-sec-hov: #e5e7eb;
  }
  [data-theme="pokemon"] {
    --bg: #f5f5f5;
    --panel: #ffffff;
    --border: #222224;
    --accent: #cc0000;
    --accent-glow: 0 0 10px rgba(204,0,0,0.5);
    --danger: #ff0000;
    --warn: #ffcb05; /* pikachu yellow */
    --ok: #4dad5b;
    --text: #222224;
    --muted: #555555;
    --mono: 'SFMono-Regular', Menlo, Consolas, monospace;
    
    --topbar-bg: #cc0000dd; /* red topbar */
    --shadow-base: rgba(0,0,0,0.15);
    --shadow-dim: rgba(0,0,0,0.1);
    --input-bg: #ffffff;
    --input-focus: #fffae5; /* yellow tint */
    --btn-bg: #ffffff;
    --btn-hover: #fffae5;
    --danger-bg: #ffeeee;
    --danger-hover: #ffdddd;
    --danger-border-hover: #ff0000;
    --notice-bg: #fffceb;
    --item-bg: #ffffff;
    --item-hover: #fafafa;
    --scenario-bg: #ffffff;
    --scenario-shadow: rgba(0,0,0,0.05);
    --count-bg: #e0e0e0;
    --info-btn-bg: #f5f5f5;
    --info-btn-hover: #eeeeee;
    --info-btn-focus: #e0e0e0;
    --info-btn-shadow: #cccccc;
    --pop-bg: #ffffff;
    --pop-shadow: rgba(0,0,0,0.2);
    --status-bg: #eeeeee;
    --status-run: #e8f5e9;
    --status-stop: #ffebee;
    --action-bg: #ffffffdd;
    --dock-bg: #fafafa;
    --dock-hdr: #eeeeee;
    --dock-hov: #e0e0e0;
    --log-bg: #ffffff;
    --log-succ: #e8f5e9;
    --log-warn: #fff8e1;
    --log-err: #ffebee;
    --prog-over: rgba(255,255,255,.9);
    --prog-card: #ffffff;
    --prog-bar-bg: #eeeeee;
    --btn-sec: #f5f5f5;
    --btn-sec-hov: #eeeeee;
  }
  [data-theme="mario"] {
    --bg: #5c94fc; /* sky blue */
    --panel: #ffffff;
    --border: #000000;
    --accent: #e4000f; /* mario red */
    --accent-glow: 0 0 0 transparent;
    --danger: #c84c0c; /* brick brown */
    --warn: #fbd000; /* coin yellow */
    --ok: #00b139; /* luigi green */
    --text: #000000;
    --muted: #767676;
    --mono: "Courier New", Courier, monospace;
    
    --topbar-bg: #e4000fdd; /* red topbar */
    --shadow-base: rgba(0,0,0,0.3);
    --shadow-dim: rgba(0,0,0,0.2);
    --input-bg: #ffffff;
    --input-focus: #fbd000; /* yellow focus */
    --btn-bg: #ffffff;
    --btn-hover: #eeeeee;
    --danger-bg: #ffffff;
    --danger-hover: #eeeeee;
    --danger-border-hover: #c84c0c;
    --notice-bg: #fbd00055;
    --item-bg: #ffffff;
    --item-hover: #eeeeee;
    --scenario-bg: #ffffff;
    --scenario-shadow: rgba(0,0,0,0.1);
    --count-bg: #eeeeee;
    --info-btn-bg: #ffffff;
    --info-btn-hover: #eeeeee;
    --info-btn-focus: #dddddd;
    --info-btn-shadow: #000000;
    --pop-bg: #ffffff;
    --pop-shadow: rgba(0,0,0,0.4);
    --status-bg: #eeeeee;
    --status-run: #d4f8d4;
    --status-stop: #f8d4d4;
    --action-bg: #ffffffdd;
    --dock-bg: #ffffff;
    --dock-hdr: #eeeeee;
    --dock-hov: #dddddd;
    --log-bg: #ffffff;
    --log-succ: #d4f8d4;
    --log-warn: #fff4d4;
    --log-err: #f8d4d4;
    --prog-over: rgba(92,148,252,.8);
    --prog-card: #ffffff;
    --prog-bar-bg: #eeeeee;
    --btn-sec: #ffffff;
    --btn-sec-hov: #eeeeee;
  }
  
  [data-theme="pokemon"] .topbar { color: #ffffff; border-bottom: 4px solid #111; background: linear-gradient(135deg, #cc0000 0%, #aa0000 100%); box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
  [data-theme="pokemon"] .topbar a { color: #ffcb05; text-shadow: 1px 1px 0 #111; font-weight: 800; }
  [data-theme="pokemon"] .topbar strong { color: #ffffff !important; text-shadow: 1px 1px 0 #111 !important; }
  [data-theme="pokemon"] .topbar strong::after { content: " 🔴"; text-shadow: none; font-size: 1.1em; }
  [data-theme="pokemon"] .topbar .muted { color: #eeeeee; }
  [data-theme="pokemon"] button { border-radius: 20px; border: 2px solid #111; box-shadow: 2px 2px 0px #111; font-weight: 800; transition: transform 0.1s, box-shadow 0.1s; }
  [data-theme="pokemon"] button:active { box-shadow: 0 0 0px #111; transform: translateY(2px) translateX(2px); }
  [data-theme="pokemon"] .card { border-radius: 12px; border: 3px solid #111; border-top: 15px solid #cc0000; box-shadow: 0 8px 16px rgba(0,0,0,0.2); }
  [data-theme="pokemon"] .vm-item { border-radius: 8px; border: 2px solid #ddd; }
  [data-theme="pokemon"] .vm-item:hover { border-color: #ffcb05; box-shadow: 0 4px 8px rgba(255,203,5,0.4); }
  [data-theme="pokemon"] .vm-status.running { background: #ffcb05; color: #111; border: 2px solid #111; box-shadow: none; font-weight: 800; }
  
  /* Mario Specific Styles */
  [data-theme="mario"] .topbar { color: #ffffff; border-bottom: 4px solid #000; background: #e4000f; box-shadow: 0 4px 0px rgba(0,0,0,0.4); }
  [data-theme="mario"] .topbar a { color: #ffffff; text-decoration: underline; font-weight: 800; }
  [data-theme="mario"] .topbar strong { color: #ffffff !important; font-size: 1.2em; text-shadow: 2px 2px 0px #000 !important; }
  [data-theme="mario"] .topbar strong::before { content: "🍄 "; font-size: 1em; }
  [data-theme="mario"] button { border-radius: 0; border: 3px solid #000; box-shadow: 4px 4px 0px #000; font-weight: 800; transition: transform 0.1s, box-shadow 0.1s; background: #ffffff; color: #000; }
  [data-theme="mario"] button:hover { background: #fbd000; transform: translate(-1px, -1px); box-shadow: 5px 5px 0px #000; }
  [data-theme="mario"] button:active { box-shadow: 0px 0px 0px #000; transform: translate(4px, 4px); }
  [data-theme="mario"] button:disabled { box-shadow: 2px 2px 0px #000; transform: none; opacity: 0.6; }
  [data-theme="mario"] .card { border-radius: 0; border: 4px solid #000; box-shadow: 8px 8px 0px rgba(0,0,0,0.2); }
  [data-theme="mario"] .vm-item { border-radius: 0; border: 3px solid #000; margin-bottom: 4px; box-shadow: 3px 3px 0px rgba(0,0,0,0.1); }
  [data-theme="mario"] .vm-item:hover { background: #fbd00022; border-color: #e4000f; box-shadow: 4px 4px 0px rgba(228,0,15,0.2); }
  [data-theme="mario"] .vm-status.running { background: #00b139; color: #fff; border: 2px solid #000; box-shadow: 2px 2px 0px #000; font-weight: 800; }
  [data-theme="mario"] .vm-status.stopped { background: #c84c0c; color: #fff; border: 2px solid #000; box-shadow: 2px 2px 0px #000; }
  [data-theme="mario"] .scenario-section { border: 4px solid #000; background: #ffffff; box-shadow: 6px 6px 0px rgba(0,0,0,0.1); }
  [data-theme="mario"] .scenario-header { border-bottom: 4px solid #000; color: #e4000f; font-weight: 900; }
  [data-theme="mario"] .backend-health-indicator.health-running { background: #00b139; color: #fff; border: 2px solid #000; box-shadow: 2px 2px 0px #000; }
  [data-theme="mario"] .backend-health-indicator.health-error { background: #c84c0c; color: #fff; border: 2px solid #000; box-shadow: 2px 2px 0px #000; }

  * { box-sizing: border-box; }
  html, body { height:100%; }
  body { font-family: var(--mono); margin:0; padding:0 1.25rem 0; background: var(--bg); color: var(--text); min-height:100vh; display:flex; flex-direction:column; text-shadow: none; }
  [data-theme="hacker"] body { text-shadow: 0 0 2px var(--muted); }
  .topbar { position:sticky; top:0; backdrop-filter: blur(6px); background:var(--topbar-bg); border-bottom: 1px solid var(--border); padding:0.9rem 1rem; margin:0 -1.25rem 1rem; display:flex; justify-content:space-between; align-items:center; color:var(--text); box-shadow:0 0 8px var(--shadow-base); }
  .topbar strong { letter-spacing:.5px; font-weight:600; text-transform:uppercase; text-shadow: var(--accent-glow); }
  .topbar a { color:var(--accent); text-shadow: var(--accent-glow); }
  [data-theme="original"] .topbar strong, [data-theme="original"] .topbar a, [data-theme="original"] h2, [data-theme="original"] h3 { text-shadow: none; }
  
  a { text-decoration:none; color: var(--accent); }
  a:hover { text-decoration:underline; text-shadow: var(--accent-glow); }
  .card { width:100%; max-width:1100px; margin:0 auto 1.2rem; border:1px solid var(--border); background:var(--panel); border-radius:0; padding:1.4rem 1.5rem 1.8rem; box-shadow:0 0 10px var(--shadow-dim); flex:1; display:flex; flex-direction:column; }
  h2,h3 { margin-top:0; font-weight:600; letter-spacing:.5px; text-transform:uppercase; text-shadow: var(--accent-glow); }
  input, select, button { font: inherit; line-height:1.2; font-family: var(--mono); }
  input, select { width:100%; padding:.55rem .65rem; border:1px solid var(--border); border-radius:0; background:var(--input-bg); color:var(--text); margin-bottom:.7rem; }
  input:focus, select:focus { outline:none; box-shadow: var(--accent-glow); border-color:var(--accent); background:var(--input-focus); }
  button { padding:.6rem 1.1rem; border:1px solid var(--accent); background:var(--btn-bg); color:var(--accent); border-radius:0; font-weight:600; letter-spacing:.4px; display:inline-flex; gap:.35rem; align-items:center; text-transform:uppercase; cursor:pointer; text-shadow:0 0 2px var(--accent); }
  [data-theme="original"] button, [data-theme="pokemon"] button { text-shadow:none; }
  button:hover { background:var(--btn-hover); box-shadow:var(--accent-glow); }
  button:active { transform:translateY(1px); }
  button:disabled { opacity: 0.5; cursor: not-allowed; border-color:var(--muted); color:var(--muted); text-shadow:none; }
  .btn-danger { border-color:var(--danger); background:var(--danger-bg); color:var(--danger); text-shadow:0 0 2px var(--danger); box-shadow:none; }
  [data-theme="original"] .btn-danger, [data-theme="pokemon"] .btn-danger { text-shadow:none; }
  .btn-danger:hover { background:var(--danger-hover); border-color:var(--danger-border-hover); box-shadow: 0 0 8px var(--danger); }
  .row { display:flex; gap:.75rem; }
  .row > * { flex:1; }
  .error { color: var(--danger); margin-bottom:.8rem; font-weight:500; text-shadow:0 0 4px var(--danger); }
  [data-theme="original"] .error, [data-theme="pokemon"] .error { text-shadow:none; }
  .notice { background:var(--notice-bg); border:1px solid var(--border); padding:.6rem .75rem; border-radius:0; margin-bottom:1rem; font-size:.9rem; text-shadow:none; }
  .vm-list { display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:.65rem; margin:0 0 1rem; }
  .vm-item { position:relative; display:flex; align-items:flex-start; gap:.5rem; border:1px solid var(--muted); border-radius:0; padding:.55rem 3.1rem .55rem 2.2rem; background:var(--item-bg); min-height:60px; overflow:hidden; cursor:pointer; transition:border-color .18s, box-shadow .18s, background .25s; }
  .vm-item:hover { border-color:var(--border); background:var(--item-hover); box-shadow:0 0 8px var(--shadow-base); }
  .scenario-section { border: 1px solid var(--muted); background: var(--scenario-bg); border-radius: 0; padding: 1rem; margin-bottom: 2rem; box-shadow: inset 0 0 10px var(--scenario-shadow); }
  .scenario-header { font-size: 0.85rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--accent); margin: 0 0 0.8rem 0.2rem; padding-bottom: 0.4rem; border-bottom: 1px solid var(--muted); display: flex; align-items: center; gap: 0.5rem; text-shadow:0 0 2px var(--accent); }
  [data-theme="original"] .scenario-header, [data-theme="pokemon"] .scenario-header { text-shadow:none; }
  .vm-list { margin: 0; }
  .scenario-btn { margin-left: auto; padding: 0.35rem 0.8rem; font-size: 0.72rem; background: var(--danger-bg); color: var(--danger); border: 1px solid var(--danger); border-radius: 0; cursor: pointer; font-weight: 700; transition: all 0.2s; letter-spacing: 0.3px; text-shadow: none; box-shadow: 0 2px 4px var(--shadow-base); }
  .scenario-btn:hover { background: var(--danger-hover); box-shadow: 0 0 8px var(--danger); transform: translateY(-1px); border-color:var(--danger-border-hover); }
  .scenario-count { background: var(--count-bg); color: var(--text); padding: 0.1rem 0.4rem; border-radius: 0; border:1px solid var(--muted); font-size: 0.7rem; }
  .vm-item input[type=checkbox] { position:absolute; left:.65rem; top:.75rem; width:1.05rem; height:1.05rem; margin:0; accent-color: var(--accent); cursor:pointer; }
  .vm-info-btn { position:absolute; top:0; right:0; height:100%; width:2.6rem; border:0; border-left:1px solid var(--muted); background:var(--info-btn-bg); color:var(--text); font-weight:700; font-size:1.4rem; display:flex; align-items:center; justify-content:center; cursor:pointer; transition: all .2s; box-shadow:inset 0 0 0 1px var(--info-btn-shadow); }
  .vm-info-btn:hover { background:var(--info-btn-hover); color:var(--accent); text-shadow: var(--accent-glow); border-left-color:var(--accent); }
  [data-theme="original"] .vm-info-btn:hover, [data-theme="pokemon"] .vm-info-btn:hover { text-shadow:none; }
  .vm-info-btn:focus { outline:1px solid var(--accent); background:var(--info-btn-focus); outline-offset:1px; }
  .vm-notes-pop { position:fixed; z-index:12000; background:var(--pop-bg); color:var(--text); border:1px solid var(--border); border-radius:0; padding:.65rem .75rem .7rem; width:320px; max-width:80vw; box-shadow:0 0 20px var(--pop-shadow); display:none; }
  .vm-notes-pop.visible { display:block; }
  .vm-notes-pop h5 { margin:0 0 .35rem; font-size:.72rem; letter-spacing:.5px; text-transform:uppercase; color:var(--accent); display:flex; justify-content:space-between; align-items:center; text-shadow:0 0 3px var(--accent); }
  [data-theme="original"] .vm-notes-pop h5, [data-theme="pokemon"] .vm-notes-pop h5 { text-shadow:none; }
  .vm-notes-pop pre { margin:0; white-space:pre-wrap; font-family:var(--mono); font-size:.72rem; color:var(--text); text-shadow:none; }
  .vm-notes-close { background:transparent; border:1px solid var(--border); color:var(--accent); font-size:.65rem; padding:.1rem .35rem; border-radius:0; cursor:pointer; text-transform:uppercase; }
  .vm-notes-close:hover { background:var(--info-btn-hover); box-shadow:var(--accent-glow); }
  .vm-item a { display:flex; flex-direction:column; gap:.25rem; color:inherit; flex:1; text-decoration:none; }
  .vm-id-line { font:600 .85rem var(--mono); letter-spacing:.5px; color:var(--muted); text-shadow:none; }
  .vm-name { font-weight:600; font-size:.95rem; line-height:1.1; margin-top:0.2rem; }
  .vm-status { font-size:.72rem; font-weight:600; letter-spacing:1px; text-transform:uppercase; display:inline-block; padding:.17rem .45rem; border-radius:0; background:var(--status-bg); color:var(--muted); box-shadow:inset 0 0 0 1px var(--muted); margin-top:0.2rem; }
  .vm-status.running { background:var(--status-run); color:var(--text); box-shadow:inset 0 0 0 1px var(--border); }
  .vm-status.stopped, .vm-status.paused { background:var(--status-stop); color:var(--danger); box-shadow:inset 0 0 0 1px var(--danger); }
  .vm-status.changed { outline:1px solid var(--accent); animation: pulse 1.1s ease-out; }
  @keyframes pulse { 0% { transform:scale(.9); filter:brightness(1.4);} 70% { transform:scale(1.03);} 100% { transform:scale(1); filter:brightness(1);} }
  .bulk-actions { display:flex; gap:.6rem; flex-wrap:wrap; }
  .with-side { display:block; }
  .vm-action-layout { display:grid; grid-template-columns: 1fr 210px; gap:1.25rem; align-items:start; }
  @media (max-width:1050px){ .vm-action-layout { grid-template-columns:1fr; } .action-frame { position:relative; top:auto; } }
  .action-frame { position:sticky; top:68px; display:flex; flex-direction:column; gap:.65rem; background:var(--action-bg); backdrop-filter:blur(8px); padding:.9rem .95rem 1.1rem; border:1px solid var(--border); border-radius:0; box-shadow:inset 0 0 10px var(--shadow-dim); min-height:140px; }
  .action-frame h4 { margin:0 0 .4rem; font-size:.68rem; letter-spacing:.55px; font-weight:600; text-transform:uppercase; color:var(--accent); text-align:center; text-shadow:0 0 3px var(--accent); }
  [data-theme="original"] .action-frame h4, [data-theme="pokemon"] .action-frame h4 { text-shadow:none; }
  .action-frame .btn-group { display:flex; flex-direction:column; gap:.45rem; }
  .action-frame button { width:100%; justify-content:center; min-height:38px; }
  .action-frame .small-group { display:flex; gap:.4rem; }
  .action-frame .small-group button { flex:1; min-height:32px; font-size:.65rem; }
  .activity-frame { margin:1.2rem 0 0; border:1px solid var(--border); background:var(--action-bg); backdrop-filter:blur(8px); border-radius:0; box-shadow:inset 0 0 10px var(--shadow-dim); padding:.4rem 0 .2rem; display:flex; flex-direction:column; }
  .activity-frame h4 { margin:.2rem .9rem .4rem; font-size:.7rem; letter-spacing:.6px; text-transform:uppercase; font-weight:600; color:var(--accent); display:flex; justify-content:center; gap:.75rem; align-items:center; text-shadow:0 0 3px var(--accent); }
  [data-theme="original"] .activity-frame h4, [data-theme="pokemon"] .activity-frame h4 { text-shadow:none; }
  .activity-frame h4 button { position:static; width:auto; }
  .activity-dock { position:relative; background:var(--dock-bg); color:var(--text); font-size:.72rem; font-family:var(--mono); height:28vh; max-height:60vh; min-height:34px; border:1px solid var(--border); border-radius:0; box-shadow:none; margin:0 auto; width:100%; max-width:1100px; overflow:hidden; }
  .activity-dock.collapsed { height:34px !important; min-height:34px; }
  .dock-resize-handle { position:absolute; top:0; left:0; right:0; height:6px; cursor:ns-resize; background:var(--muted); opacity:.6; }
  .activity-dock.resizing { user-select:none; }
  .activity-dock .dock-header { padding:.35rem .75rem; display:flex; justify-content:space-between; align-items:center; font-weight:600; letter-spacing:.5px; background:var(--dock-hdr); border-bottom:1px solid var(--border); text-transform:uppercase; text-shadow:0 0 2px var(--accent); }
  [data-theme="original"] .activity-dock .dock-header, [data-theme="pokemon"] .activity-dock .dock-header { text-shadow:none; }
  .dock-last { flex:1; font-weight:400; font-size:.65rem; color:var(--muted); margin:0 .65rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; text-shadow:none; }
  .activity-dock .dock-toggle { background:transparent; color:var(--accent); border:1px solid var(--accent); padding:.15rem .55rem; border-radius:0; font-size:.65rem; line-height:1; cursor:pointer; }
  .activity-dock .dock-toggle:hover { background:var(--dock-hov); box-shadow:var(--accent-glow); }
  .activity-dock .dock-clear { background:transparent; color:var(--accent); border:1px solid var(--accent); padding:.15rem .55rem; border-radius:0; font-size:.65rem; line-height:1; cursor:pointer; margin-right:.35rem; }
  .activity-dock .dock-clear:hover { background:var(--dock-hov); box-shadow:var(--accent-glow); }
  .activity-dock .dock-body { padding:.4rem .6rem .7rem; overflow-y:auto; overflow-x:hidden; display:flex; flex-direction:column; gap:.25rem; }
  .log-line { padding:.15rem .4rem; border-radius:0; background:var(--log-bg); border-left:2px solid var(--muted); }
  .log-line.info { background:var(--log-bg); border-left-color:var(--muted); }
  .log-line.success { background:var(--log-succ); border-left-color:var(--border); color:#fff; text-shadow:0 0 2px #fff; }
  [data-theme="original"] .log-line.success, [data-theme="pokemon"] .log-line.success { color:var(--text); text-shadow:none; border-left-color:var(--ok); }
  .log-line.warn { background:var(--log-warn); border-left-color:var(--warn); color:var(--warn); }
  .log-line.error { background:var(--log-err); border-left-color:var(--danger); color:var(--danger); text-shadow:0 0 2px var(--danger); }
  [data-theme="original"] .log-line.error, [data-theme="pokemon"] .log-line.error { text-shadow:none; }
  .muted { color:var(--muted); text-shadow:none; }
  code { font-family:var(--mono); font-size:.85rem; background:var(--btn-sec); color:var(--border); padding:.15rem .4rem; border-radius:0; border:1px solid var(--muted); text-shadow:none; }
  footer { margin-top:1.5rem; text-align:center; font-size:.7rem; color:var(--muted); text-transform:uppercase; text-shadow:none; }
  .divider { height:1px; background:linear-gradient(90deg,transparent,var(--muted),transparent); margin:1.2rem 0; border-radius:0; }
  .inline-form { display:inline; }
  .actions-row { margin-top:.4rem; }
  .vm-item:focus-within { outline:1px solid var(--accent); box-shadow:var(--accent-glow); }
  @media (max-width:640px){ .vm-list { grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); } }
  .progress-overlay { position:fixed; inset:0; background:var(--prog-over); display:flex; align-items:center; justify-content:center; z-index:20000; opacity:0; visibility:hidden; pointer-events:none; transition:opacity .12s ease-out; backdrop-filter: blur(2px); }
  .progress-overlay.visible { opacity:1; visibility:visible; pointer-events:auto; }
  .progress-card { background:var(--prog-card); color:var(--text); border:1px solid var(--border); border-radius:0; padding:1.4rem 1.5rem; min-width:360px; max-width:92vw; box-shadow:0 0 20px var(--pop-shadow); display:flex; flex-direction:column; gap:.75rem; text-align:center; font-family:var(--mono); }
  .progress-title { font-weight:800; letter-spacing:.4px; font-size:1.05rem; color:var(--accent); text-transform:uppercase; text-shadow:0 0 4px var(--accent); }
  [data-theme="original"] .progress-title, [data-theme="pokemon"] .progress-title { text-shadow:none; }
  .progress-msg { font-size:.9rem; color:var(--text); font-weight:600; text-shadow:none; }
  .progress-bar { height:6px; border-radius:0; background:var(--prog-bar-bg); overflow:hidden; border:1px solid var(--muted); }
  .progress-bar span { display:block; height:100%; width:40%; background:var(--accent); box-shadow:0 0 8px var(--accent); animation: progress-indef 1.1s ease-in-out infinite; }
  @keyframes progress-indef { 0%{ transform:translateX(-60%);} 100%{ transform:translateX(220%);} }
  .confirm-overlay { position:fixed; inset:0; background:var(--prog-over); display:flex; align-items:center; justify-content:center; z-index:21000; opacity:0; visibility:hidden; pointer-events:none; transition:opacity .12s ease-out; backdrop-filter: blur(2px); }
  .confirm-overlay.visible { opacity:1; visibility:visible; pointer-events:auto; }
  .confirm-card { background:var(--prog-card); color:var(--text); border:1px solid var(--border); border-radius:0; padding:1.2rem 1.4rem; min-width:340px; max-width:92vw; box-shadow:0 0 20px var(--pop-shadow); display:flex; flex-direction:column; gap:.75rem; text-align:center; font-family:var(--mono); }
  .confirm-title { font-weight:800; letter-spacing:.4px; font-size:1rem; color:var(--accent); text-transform:uppercase; text-shadow:0 0 4px var(--accent); }
  [data-theme="original"] .confirm-title, [data-theme="pokemon"] .confirm-title { text-shadow:none; }
  .confirm-msg { font-size:.85rem; color:var(--text); font-weight:600; white-space:pre-wrap; text-shadow:none; }
  .confirm-actions { display:flex; gap:.6rem; justify-content:center; }
  .btn-secondary { border-color:var(--muted); background:var(--btn-sec); box-shadow:none; color:var(--muted); text-shadow:none; }
  .btn-secondary:hover { background:var(--btn-sec-hov); border-color:var(--accent); color:var(--accent); box-shadow:0 0 8px var(--muted); }
  
  /* Backend Health Indicator */
  .backend-health-indicator {
    margin-left: 10px;
    font-size: 0.75rem;
    font-weight: 600;
    padding: 0.2rem 0.5rem;
    border-radius: 0;
    display: inline-block;
    border: 1px solid var(--muted);
    background-color: var(--status-bg);
    color: var(--muted);
  }
  .backend-health-indicator.health-running {
    background-color: var(--status-run);
    color: var(--ok);
    border-color: var(--ok);
  }
  [data-theme="hacker"] .backend-health-indicator.health-running { text-shadow: 0 0 2px var(--ok); }
  
  .backend-health-indicator.health-error {
    background-color: var(--danger);
    color: #fff;
    border-color: var(--danger);
    font-weight: 800;
  }
  [data-theme="hacker"] .backend-health-indicator.health-error { text-shadow: 0 0 2px #fff; }
  
  .backend-health-indicator.health-warn {
    background-color: var(--warn);
    color: #000;
    border-color: var(--warn);
    font-weight: 800;
  }
  
  /* Login Page Advanced Section */
  .login-advanced-details {
    margin: .6rem 0 .2rem;
    border: 1px solid var(--border);
    padding: .6rem .75rem .75rem;
    border-radius: 8px;
    background: var(--panel);
  }
  [data-theme="pokemon"] .login-advanced-details { border-width: 2px; border-color: #111; }
  
  .ssl-checkbox-wrapper {
    display: flex;
    align-items: center;
    padding: .48rem .55rem;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--input-bg);
    height: 38px;
    line-height: 1;
  }
  [data-theme="pokemon"] .ssl-checkbox-wrapper { border-width: 2px; border-color: #111; }
</style>
</head>
<body>
  <div class="grid-overlay"></div>
  <div class="topbar">
    <div><strong style="color:var(--accent);">_AccessForge</strong></div>
    <div style="display:flex; align-items:center; gap:0.75rem;">
      <select id="themeSelector" onchange="changeTheme(this.value)" style="margin:0; padding:0.15rem 0.35rem; width:auto; font-size:0.75rem; cursor:pointer;" aria-label="Select Theme">
        <option value="hacker">Hacker (Default)</option>
        <option value="original">Original</option>
        <option value="pokemon">Pokemon</option>
        <option value="mario">Super Mario</option>
      </select>
      <script>
        var sel = document.getElementById('themeSelector');
        var cur = document.documentElement.getAttribute('data-theme') || '__DEFAULT_THEME__';
        if(sel) sel.value = cur;
      </script>
      {% if session.get('pve_user') %}
        <span class="muted">{{ session.get('pve_user') }}</span> |
        <a href="{{ url_for('logout') }}">Logout</a>
      {% endif %}
    </div>
  </div>
  <div class="card">
    {% block content %}{% endblock %}
  </div>
  <div id="progressOverlay" class="progress-overlay" role="dialog" aria-modal="true" aria-live="polite" aria-hidden="true">
    <div class="progress-card">
      <div class="progress-title">Working...</div>
      <div id="progressMessage" class="progress-msg">Please wait.</div>
      <div class="progress-bar" aria-hidden="true"><span></span></div>
    </div>
  </div>
</body>
</html>
"""

# Register in-memory base template for Jinja to resolve `{% extends "base.html" %}`
app.jinja_loader = DictLoader({"base.html": TPL_BASE.replace("__DEFAULT_THEME__", APP_DEFAULT_THEME)})

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
  # Prevent stale HTML from being cached by the browser during rapid UI changes
  if resp.mimetype == 'text/html':
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
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
  <details class="login-advanced-details">
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
        <div class="ssl-checkbox-wrapper">
          <input id="verifyBox" aria-label="Verify SSL" type="checkbox" name="verify_ssl" value="1" {% if verify_ssl %}checked{% endif %} style="margin:0; width:1.05rem; height:1.05rem; cursor:pointer;"/>
          <span style="font-size:.62rem; margin-left:.55rem; color:var(--muted); font-weight:500;">Uncheck only for self-signed certs</span>
        </div>
      </div>
    </div>
  </details>
  <button type="submit">Sign in</button>
</form>
<script>
 (function(){
   const u=document.getElementById('usernameInput');
   if(u){
     function extract(){
       const val=u.value.trim();
       if(val.includes('@')){
         const parts=val.split('@');
         if(parts.length > 0 && parts[0]){
           u.value=parts[0];
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
    <label style="display:flex; align-items:center; gap:.3rem; font-size:.72rem; cursor:pointer;" title="Automatically refresh VM statuses every 5 minutes">
      <input type="checkbox" id="autoRefreshToggle" checked style="margin:0; width:1.05rem; height:1.05rem;" />
      Auto-refresh (5m)
    </label>
    <small class="muted" id="refreshMeta" aria-live="polite" style="margin-left: .5rem; border-left: 1px solid var(--border); padding-left: .5rem;">Last refresh: never</small>
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
    {% for scenario, group_vms in grouped_vms.items() %}
    <div class="scenario-section">
      <div class="scenario-header">
        {{ scenario }}
        <span class="scenario-count">{{ group_vms|length }}</span>
        {% if backend_health.get(scenario) %}
          {% set b_health = backend_health[scenario] %}
          <span id="health-{{ scenario|replace(' ', '-') }}" class="backend-health-indicator {% if b_health == 'Running' %}health-running{% elif 'ERROR' in b_health %}health-error{% else %}health-warn{% endif %}">
            Backend Network: <span class="health-text">{{ b_health }}</span>
          </span>
        {% endif %}
        <button type="button" class="scenario-btn btn-scenario-reset" data-scenario="{{ scenario }}" title="Reset ONLY backend VMs for this scenario">Reset Network Backend</button>
      </div>
      <div class="vm-list">
      {% for vm in group_vms %}
        <label class="vm-item" data-node="{{ vm.get('node') }}" data-vmid="{{ vm.get('vmid') }}">
          <input type="checkbox" name="vms" value="{{ vm.get('node') }}|{{ vm.get('type') }}|{{ vm.get('vmid') }}" />

          <button type="button" class="vm-info-btn" title="View notes" aria-label="View notes" data-node="{{ vm.get('node') }}" data-type="{{ vm.get('type') }}" data-vmid="{{ vm.get('vmid') }}">📄</button>
          <a href="{{ url_for('open_console') }}?node={{ vm.get('node') }}&vmid={{ vm.get('vmid') }}&vtype={{ vm.get('type') }}" target="_blank" rel="noopener" data-node="{{ vm.get('node') }}" data-vmid="{{ vm.get('vmid') }}" data-vtype="{{ vm.get('type') }}">
            <span class="vm-id-line">#{{ vm.get('vmid') }} - {{ vm.get('node') }}</span>
            <span class="vm-name">{{ vm.get('name','') or '(no name)' }}</span>
            <span class="vm-status {{ vm.get('status') }}" id="vm-status-{{ vm.get('node') }}-{{ vm.get('vmid') }}">{{ vm.get('status') }}</span>
          </a>
        </label>
      {% endfor %}
      </div>
    </div>
    {% else %}
      {% if not vms %}
        <p class="muted">No VMs listed, or your account lacks VM.Audit permission.</p>
      {% endif %}
    {% endfor %}

    <form method="post" action="{{ url_for('bulk_action') }}" id="bulkForm">
      <input type="hidden" name="action" value="" id="hiddenBulkAction" />
      <input type="hidden" name="snapshot" value="" id="hiddenSnapshot" />
      <input type="hidden" name="vms_visible" value="" id="hiddenVisibleVms" />
      <input type="hidden" name="scenario" value="" id="hiddenScenario" />
    </form>
  </div>
  <div class="action-frame" aria-label="Bulk VM Actions">
    <h4>Selection Actions</h4>
    <p style="margin:-0.2rem 0 0.6rem; font-size:0.65rem; color:#9cc9d9; text-align:center;">Applied to checked VMs only</p>
    <div class="btn-group">
  <button id="btnStart" type="button" disabled title="Start each selected VM">Start Selected</button>
  <button id="btnPoweroff" type="button" class="btn-danger" disabled title="Power off (stop) each selected VM">Poweroff Selected</button>
  <button id="btnRestore" type="button" class="btn-danger" disabled title="Rollback each selected VM to its newest snapshot">Factory Reset Selected</button>
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
<div id="activityDock" class="activity-dock" aria-label="Activity Log Panel">
  <div class="dock-resize-handle" title="Drag to resize"></div>
  <div class="dock-header">Activity Log <span id="dockLastLine" class="dock-last"></span>
    <div style="margin-left:auto; display:flex; gap:.35rem; align-items:center">
      <button type="button" id="dockClear" class="dock-clear" aria-label="Clear Activity Log">Clear</button>
      <button type="button" id="dockToggle" class="dock-toggle" aria-expanded="true" aria-controls="dockBody" aria-label="Toggle Activity">v</button>
    </div>
  </div>
  <div id="dockBody" class="dock-body" role="log" aria-live="polite"></div>
</div>
</div>
{% endif %}
<div id="appConfig" data-api-vms="{{ url_for('api_vms') }}" data-session-reset="{{ url_for('session_reset', reason='invalid') }}" data-jobs-status="{{ url_for('api_jobs_status') }}" data-notes-url="{{ url_for('api_vm_notes') }}" style="display:none"></div>
<script src="/static/app.js" defer></script>
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


def _extract_scenario(notes: str) -> str:
  if not notes:
    return "Uncategorized"
  
  # Decode HTML entities (Proxmox sometimes sends &quot; for quotes, etc)
  try:
    clean_notes = html.unescape(notes)
  except:
    clean_notes = notes

  # Remove HTML tags if present (e.g. <br>, <div>)
  clean_notes = re.sub(r'<[^>]+>', ' ', clean_notes)

  # Look for "Scenario": "Value" or Scenario: Value
  # We use DOTALL to allow matching across lines if needed, though usually key/value is on one line.
  # We look for Key... : ... Value
  match = re.search(r'[\"\']?Scenario[\"\']?\s*[:=]\s*[\"\']?([^\"\',}\r\n]+)[\"\']?', clean_notes, re.IGNORECASE)
  if match:
    return match.group(1).strip()
  
  return "Uncategorized"

def fetch_vm_notes(vm, cookies, headers, host, port, verify):
  """
  Fetches config for a single VM to get its notes/description.
  Returns (vmid, notes_text).
  """
  try:
    node = vm.get("node")
    vmid = vm.get("vmid")
    vtype = vm.get("type", "qemu") # default to qemu if missing (unlikely for valid VMs)
    if vtype not in ("qemu", "lxc"):
       return (vmid, "")
  
    # Use requests directly to avoid session context issues in threads if any, 
    # but we need the base URL.
    # We will pass specific cookies/headers.
    
    path = f"/nodes/{node}/{vtype}/{vmid}/config"
    url = f"https://{host}:{port}/api2/json" + path
    
    
    # Enable verbose logging for debugging
    # logger.info(f"DEBUG: Fetching notes for {vmid} from {url}")
    resp = requests.get(url, cookies=cookies, headers=headers, verify=verify, timeout=5)
    
    if resp.ok:
      data = resp.json().get("data", {})
      notes = data.get("description", "")
      # logger.info(f"DEBUG: VM {vmid} raw notes: {notes!r}")
      
      extracted = _extract_scenario(notes)
      # logger.info(f"DEBUG: VM {vmid} extracted scenario: {extracted!r}")
      return (vmid, notes)
    else:
      logger.warning(f"DEBUG: Failed to fetch notes for {vmid}: status={resp.status_code}")
  except Exception as e:
    logger.exception(f"DEBUG: Exception fetching notes for {vmid}: {e}")
  return (vm.get("vmid"), "")


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
  # Remove realm from username if provided inline
  if "@" in username_input and not username_input.startswith("@"):  # basic guard
    parts = username_input.split("@", 1)
    username = parts[0].strip()
  else:
    username = username_input

  password = request.form.get("password") or ""
  host_override = (request.form.get("host") or "").strip() or PROXMOX_HOST
  # Use provided port or default to configured PROXMOX_PORT (do not auto-switch 8006->443)
  port_override = (request.form.get("port") or "").strip() or PROXMOX_PORT
  realm_field = (request.form.get("realm") or "").strip() or PROXMOX_REALM
  realm_override = realm_field
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
  raw_vms = []
  raw_vms_by_id = {}
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
      raw_vms = [row for row in r.json().get("data", []) if row.get("type") in ("qemu", "lxc") and not row.get("template")]
      raw_vms_by_id = {str(vm["vmid"]): vm for vm in raw_vms}
      
      # Filter by VM.Console permission to hide backend VMs
      try:
          p = proxmox_get("/access/permissions", cookies=cookies, headers=headers)
          if p.ok:
              perms = p.json().get("data", {})
              for vm in raw_vms:
                  vmid = str(vm.get("vmid"))
                  vm_perms = perms.get(f"/vms/{vmid}", {})
                  # Only show VMs where the user has VM.Console
                  if "VM.Console" in vm_perms:
                      vms.append(vm)
          else:
              logger.warning(f"[{req_id()}] Failed to fetch permissions: {p.status_code}")
              vms = raw_vms # fallback to all visible if perm check fails
      except Exception as p_ex:
          logger.exception(f"[{req_id()}] Exception fetching permissions")
          vms = raw_vms
    else:
      logger.warning(
        f"[{req_id()}] Failed to list VMs: status={r.status_code} body={r.text[:300]!r}"
      )
  except Exception:
    logger.exception(f"[{req_id()}] Exception while listing VMs")

  # Enrich VMs with notes in parallel to determine Scenario
  # Limit workers to avoid hammering Proxmox
  grouped_vms = {}
  backend_health = {}
  if raw_vms:
    # Prepare shared auth for threads
    # Session is thread-local in Flask, but we are inside the request thread here spawning workers.
    # We pass explicit dicts to workers.
    t_cookies = {"PVEAuthCookie": session.get("pve_ticket")}
    t_headers = {"CSRFPreventionToken": session.get("pve_csrf")}
    t_host = session.get("pve_host", PROXMOX_HOST)
    t_port = session.get("pve_port", PROXMOX_PORT)
    t_verify = session.get("pve_verify_ssl", VERIFY_SSL)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
      # Submit tasks for all raw_vms so we can accurately find hidden backend VMs by Scenario
      future_to_vmid = {
        executor.submit(fetch_vm_notes, vm, t_cookies, t_headers, t_host, t_port, t_verify): str(vm["vmid"]) 
        for vm in raw_vms
      }
      for future in concurrent.futures.as_completed(future_to_vmid):
        vmid = future_to_vmid[future]
        try:
          _, notes = future.result()
          raw_vms_by_id[vmid]["notes"] = notes
          raw_vms_by_id[vmid]["scenario"] = _extract_scenario(notes)
        except Exception:
          raw_vms_by_id[vmid]["scenario"] = "Uncategorized"

    # Grouping only visible VMs
    for vm in vms:
      sc = vm.get("scenario", "Uncategorized")
      logger.info(f"DEBUG: MainThread Grouping VM {vm.get('vmid')} -> Scenario: {sc!r}")
      
      if sc not in grouped_vms:
        grouped_vms[sc] = []
      grouped_vms[sc].append(vm)
      
    # Sort groups
    sorted_keys = sorted(grouped_vms.keys())
    if "Uncategorized" in sorted_keys and len(sorted_keys) > 1:
        sorted_keys.remove("Uncategorized")
        sorted_keys.append("Uncategorized")
    grouped_vms = {k: grouped_vms[k] for k in sorted_keys}

    # Calculate backend network health for each scenario
    visible_vmid_set = set(str(v["vmid"]) for v in vms)
    backend_map = {}
    for sc, group_vms in grouped_vms.items():
      backend_vms_in_sc = set()
      # 1. Look for BackendVMs JSON inside visible VMs
      for vm in group_vms:
        notes = vm.get("notes", "")
        try:
          clean_notes = html.unescape(notes)
          clean_notes = re.sub(r'<[^>]+>', ' ', clean_notes)
          json_match = re.search(r'(\{.*BackendVMs.*\})', clean_notes, re.DOTALL | re.IGNORECASE)
          if json_match:
            note_data = json.loads(json_match.group(1))
            b_ids = note_data.get("BackendVMs", [])
            for bid in b_ids:
              backend_vms_in_sc.add(str(bid))
        except Exception:
          pass
      # 2. Add hidden VMs that belong to this scenario
      for r_vm in raw_vms:
        if str(r_vm["vmid"]) not in visible_vmid_set:
          if r_vm.get("scenario") == sc:
            backend_vms_in_sc.add(str(r_vm["vmid"]))
            
      backend_map[sc] = list(backend_vms_in_sc)
      # Check statuses
      if backend_vms_in_sc:
        all_running = True
        has_error = False
        for bid in backend_vms_in_sc:
          b_vm = raw_vms_by_id.get(bid)
          if b_vm:
            status = b_vm.get("status", "")
            if status and "error" in status.lower():
              has_error = True
              break
            if status != "running":
              all_running = False
        
        if has_error:
          backend_health[sc] = "ERROR - CONTACT ADMIN"
        elif all_running:
          backend_health[sc] = "Running"
        else:
          backend_health[sc] = "unhealthy - reset recommended"

    session["backend_map"] = backend_map


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
    grouped_vms=grouped_vms,
    backend_health=backend_health,
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
    vtype = (request.form.get("vtype") or "").strip()
  else:
    node = (request.args.get("node") or "").strip()
    vmid = (request.args.get("vmid") or "").strip()
    vtype = (request.args.get("vtype") or "").strip()

  if not node or not vmid.isdigit():
    return redirect(url_for("home"))

  console_type = "lxc" if vtype == "lxc" else "kvm"

  # Route the console through our nginx proxy on this same origin using /proxmox/
  # This avoids the browser needing to reach host.docker.internal or non-443 ports.
  qs = urllib.parse.urlencode({
    "console": console_type,
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
  g.request_id = os.urandom(4).hex()
  action = (request.form.get("action") or "").lower().strip()
  selections = request.form.getlist("vms")
  snapshot = (request.form.get("snapshot") or "").strip()
  
  logger.warning(f"[{req_id()}] === BULK_ACTION STARTED === action: '{action}' | form_keys: {list(request.form.keys())} | len(vms): {len(selections)}")
  
  if not action:
    logger.warning(f"[{req_id()}] Aborting bulk_action: No action provided in form.")
    return redirect(url_for("home"))
    
  if action != "factory-reset-scenario" and not selections:
    logger.warning(f"[{req_id()}] Aborting bulk_action: Action '{action}' requires selections, but none provided.")
    return redirect(url_for("home"))
  done = 0
  failed = 0
  skipped = 0
  failure_details = []  # collect strings "node/vmid action failed (reason)"
  success_details = []  # collect strings "node/vmid action ok"
  skip_details = []     # collect strings "node/vmid skipped (reason)"
  jobs = []             # collect task upids for async tracking
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

  def _get_newest_snapshot(node: str, vtype: str, vmid: str):
    if vtype == "qemu":
      path = f"/nodes/{node}/qemu/{vmid}/snapshot"
    elif vtype == "lxc":
      path = f"/nodes/{node}/lxc/{vmid}/snapshot"
    else:
      return None
    try:
      rsn = proxmox_get(path, cookies=cookies, headers=headers)
      if rsn.status_code == 401:
        logger.info(f"[{req_id()}] Snapshot list unauthorized vmid={vmid} node={node}; forcing session reset")
        session.clear()
        return "__unauthorized__"
      if not rsn.ok:
        logger.warning(f"[{req_id()}] Snapshot list failed vmid={vmid} node={node} status={rsn.status_code} body={rsn.text[:180]!r}")
        return None
      data = rsn.json().get("data", [])
      # Exclude the implicit 'current' marker
      snaps = [s for s in data if s.get("name") and s.get("name") != "current"]
      if not snaps:
        return None
      # Prefer newest by snaptime if available
      snaps_sorted = sorted(
        snaps,
        key=lambda s: s.get("snaptime") or 0,
        reverse=True,
      )
      return snaps_sorted[0].get("name")
    except Exception:
      logger.exception(f"[{req_id()}] Snapshot list exception vmid={vmid} node={node}")
      return None
  def _get_lock_state(node: str, vtype: str, vmid: str):
    if vtype == "qemu":
      path = f"/nodes/{node}/qemu/{vmid}/status/current"
    elif vtype == "lxc":
      path = f"/nodes/{node}/lxc/{vmid}/status/current"
    else:
      return None
    try:
      rs = proxmox_get(path, cookies=cookies, headers=headers)
      if rs.status_code == 401:
        logger.info(f"[{req_id()}] Status current unauthorized vmid={vmid} node={node}; forcing session reset")
        session.clear()
        return "__unauthorized__"
      if not rs.ok:
        logger.warning(f"[{req_id()}] Status current failed vmid={vmid} node={node} status={rs.status_code} body={rs.text[:180]!r}")
        return None
      data = rs.json().get("data", {})
      return data.get("lock")
    except Exception:
      logger.exception(f"[{req_id()}] Status current exception vmid={vmid} node={node}")
      return None

  def _extract_upid(resp):
    try:
      payload = resp.json()
    except Exception:
      return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, str) and data.startswith("UPID:"):
      return data
    return None

  def _wait_for_task(node: str, upid: str, timeout: float = 120.0, interval: float = 1.0):
    deadline = time.time() + timeout
    last_status = "unknown"
    while time.time() < deadline:
      try:
        r = proxmox_get(f"/nodes/{node}/tasks/{upid}/status", cookies=cookies, headers=headers)
        if r.status_code == 401:
          logger.info(f"[{req_id()}] Task status unauthorized upid={upid} node={node}; forcing session reset")
          session.clear()
          return "__unauthorized__", None
        if not r.ok:
          return False, f"HTTP {r.status_code}"
        data = r.json().get("data", {})
        status = data.get("status") or "unknown"
        exitstatus = data.get("exitstatus")
        last_status = status
        if status == "stopped":
          if exitstatus and exitstatus != "OK":
            return False, exitstatus
          return True, exitstatus or "OK"
      except Exception:
        logger.exception(f"[{req_id()}] Task wait exception upid={upid} node={node}")
        return False, "exception"
      time.sleep(interval)
    return False, f"timeout waiting for task ({last_status})"

  def _wait_for_vm_unlock(node: str, vtype: str, vmid: str, timeout: float = 45.0, interval: float = 1.0):
    deadline = time.time() + timeout
    last_lock = None
    while time.time() < deadline:
      lock_state = _get_lock_state(node, vtype, vmid)
      if lock_state == "__unauthorized__":
        return "__unauthorized__"
      if not lock_state:
        return None
      last_lock = lock_state
      time.sleep(interval)
    return last_lock or "unknown"

  def _run_vm_action(node: str, vtype: str, vmid: str, path: str, data=None):
    lock_state = _wait_for_vm_unlock(node, vtype, vmid)
    if lock_state == "__unauthorized__":
      return "__unauthorized__", None, None
    if lock_state:
      return None, f"locked: {lock_state}", None

    r = proxmox_post(path, data=data or {}, cookies=cookies, headers=headers)
    if r.status_code == 401:
      logger.info(f"[{req_id()}] Action unauthorized vmid={vmid} node={node} path={path}; forcing session reset")
      session.clear()
      return "__unauthorized__", None, None

    upid = _extract_upid(r)
    if not r.ok:
      return r, None, upid

    if upid:
      task_ok, task_reason = _wait_for_task(node, upid)
      if task_ok == "__unauthorized__":
        return "__unauthorized__", None, upid
      if not task_ok:
        return None, f"task failed: {task_reason}", upid

    lock_state = _wait_for_vm_unlock(node, vtype, vmid)
    if lock_state == "__unauthorized__":
      return "__unauthorized__", None, upid
    if lock_state:
      return None, f"lock did not clear: {lock_state}", upid

    return r, None, upid

  def _direct_reset_vm(node: str, vtype: str, vmid: str, current_status: str = None):
    if vtype == "qemu":
      action_name = "reset" if current_status == "running" else "start"
      path = f"/nodes/{node}/qemu/{vmid}/status/{action_name}"
    elif vtype == "lxc":
      action_name = "reboot" if current_status == "running" else "start"
      path = f"/nodes/{node}/lxc/{vmid}/status/{action_name}"
    else:
      return None, f"unsupported type {vtype}", None

    return _run_vm_action(node, vtype, vmid, path, data={})

  if action == "factory-reset-scenario":
    try:
      target_scenario = request.form.get("scenario")
      visible_vms_str = request.form.get("vms_visible", "")
      visible_vmid_set = set(v.strip() for v in visible_vms_str.split(",") if v.strip())

      logger.warning(f"[{req_id()}] ACTION: factory-reset-scenario | Target Scenario: '{target_scenario}' | Visible VMs Array: '{visible_vms_str}'")

      if target_scenario:
        t_host = session.get("pve_host", PROXMOX_HOST)
        t_port = session.get("pve_port", PROXMOX_PORT)
        t_verify = session.get("pve_verify_ssl", VERIFY_SSL)
        backend_vms_to_reset = {}

        try:
          r = proxmox_get("/cluster/resources", params={"type": "vm"}, cookies=cookies, headers=headers)
          if r.ok:
            all_vms = [row for row in r.json().get("data", []) if row.get("type") in ("qemu", "lxc") and not row.get("template")]
            all_vms_by_id = {str(vm.get("vmid")): vm for vm in all_vms}

            visible_vms_to_check = []
            hidden_vms_to_check = []
            for vm in all_vms:
              vmid = str(vm.get("vmid"))
              if vmid in visible_vmid_set:
                visible_vms_to_check.append(vm)
              else:
                hidden_vms_to_check.append(vm)

            logger.info(f"[{req_id()}] Visible VMs ({len(visible_vms_to_check)}) | Hidden VMs ({len(hidden_vms_to_check)})")

            if visible_vms_to_check:
              with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                future_to_vm = {
                  executor.submit(fetch_vm_notes, vm, cookies, headers, t_host, t_port, t_verify): vm
                  for vm in visible_vms_to_check
                }
                for future in concurrent.futures.as_completed(future_to_vm):
                  vm = future_to_vm[future]
                  try:
                    _, notes = future.result()
                    sc = _extract_scenario(notes)
                    if sc == target_scenario:
                      try:
                        clean_notes = html.unescape(notes)
                        clean_notes = re.sub(r'<[^>]+>', ' ', clean_notes)
                        json_match = re.search(r'(\{.*BackendVMs.*\})', clean_notes, re.DOTALL | re.IGNORECASE)
                        if json_match:
                          note_data = json.loads(json_match.group(1))
                          backend_ids = note_data.get("BackendVMs", [])
                          if isinstance(backend_ids, list):
                            for bid in backend_ids:
                              backend_vm = all_vms_by_id.get(str(bid))
                              if backend_vm:
                                backend_vms_to_reset[str(bid)] = backend_vm
                      except Exception:
                        pass
                  except Exception:
                    pass

            if hidden_vms_to_check:
              with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                future_to_vm = {
                  executor.submit(fetch_vm_notes, vm, cookies, headers, t_host, t_port, t_verify): vm
                  for vm in hidden_vms_to_check
                }
                for future in concurrent.futures.as_completed(future_to_vm):
                  vm = future_to_vm[future]
                  try:
                    _, notes = future.result()
                    sc = _extract_scenario(notes)
                    if sc == target_scenario:
                      vmid_str = str(vm.get("vmid"))
                      backend_vms_to_reset[vmid_str] = vm
                  except Exception as ex:
                    logger.error(f"[{req_id()}] Error parsing notes for hidden VM {vm.get('vmid')}: {ex}")
        except Exception as ex:
          logger.error(f"[{req_id()}] Failed to fetch cluster resources for context: {ex}")

        if not backend_vms_to_reset:
          logger.warning(f"[{req_id()}] Checked all accessible VMs. No backend VMs discovered for scenario '{target_scenario}' via JSON or Scenario tags.")
          failed += 1
          failure_details.append("No backend VMs discovered.")

        if backend_vms_to_reset:
          logger.info(f"[{req_id()}] Executing direct reset for Backend VMs: {list(backend_vms_to_reset.keys())}")
          for vmid, vm in backend_vms_to_reset.items():
            node = vm.get("node")
            vtype = vm.get("type")
            current_status = status_map.get((node, vmid))
            if not node or vtype not in ("qemu", "lxc"):
              failed += 1
              failure_details.append(f"{vmid} scenario-reset failed (missing node/type)")
              continue

            r, error, upid = _direct_reset_vm(node, vtype, vmid, current_status=current_status)
            if r == "__unauthorized__":
              return redirect(url_for("session_reset", reason="invalid"))
            if upid:
              jobs.append({"node": node, "upid": upid})
            if error:
              failed += 1
              failure_details.append(f"{node}/{vmid} scenario-reset failed ({error})")
              continue
            if r and r.ok:
              done += 1
              success_details.append(f"{node}/{vmid} scenario-reset ok")
            else:
              status_code = r.status_code if r is not None else "unknown"
              failed += 1
              failure_details.append(f"{node}/{vmid} scenario-reset failed (HTTP {status_code})")
      else:
        logger.warning(f"[{req_id()}] Bypassed execution because target_scenario='{target_scenario}'")
    except Exception:
      failed += 1
      failure_details.append("scenario-reset exception")
      logger.exception(f"[{req_id()}] Scenario reset exception")

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
        r, error, upid = _run_vm_action(node, vtype, vmid, path, data={})
        if r == "__unauthorized__":
          return redirect(url_for("session_reset", reason="invalid"))
        if upid:
          jobs.append({"node": node, "upid": upid})
        if error:
          failed += 1
          failure_details.append(f"{node}/{vmid} poweroff failed ({error})")
          continue
        if r and r.ok:
          done += 1
          success_details.append(f"{node}/{vmid} poweroff ok")
        else:
          failed += 1
          reason = f"HTTP {r.status_code}" if r is not None else "unknown"
          failure_details.append(f"{node}/{vmid} poweroff failed ({reason})")
          if r is not None:
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
        r, error, upid = _run_vm_action(node, vtype, vmid, path, data={})
        if r == "__unauthorized__":
          return redirect(url_for("session_reset", reason="invalid"))
        if upid:
          jobs.append({"node": node, "upid": upid})
        if error:
          failed += 1
          failure_details.append(f"{node}/{vmid} start failed ({error})")
          continue
        if r and r.ok:
          done += 1
          success_details.append(f"{node}/{vmid} start ok")
        else:
          failed += 1
          reason = f"HTTP {r.status_code}" if r is not None else "unknown"
          failure_details.append(f"{node}/{vmid} start failed ({reason})")
          if r is not None:
            logger.warning(f"[{req_id()}] Start failed vmid={vmid} node={node} status={r.status_code} body={r.text[:180]!r}")
      elif action == "restore-all":
        # Roll back to a named snapshot or auto-pick newest
        snap_name = snapshot
        if not snap_name:
          snap_name = _get_newest_snapshot(node, vtype, vmid)
          if snap_name == "__unauthorized__":
            return redirect(url_for("session_reset", reason="invalid"))
        if not snap_name:
          skipped += 1
          skip_details.append(f"{node}/{vmid} skipped (no snapshots)")
          continue
        if vtype == "qemu":
          path = f"/nodes/{node}/qemu/{vmid}/snapshot/{snap_name}/rollback"
        elif vtype == "lxc":
          path = f"/nodes/{node}/lxc/{vmid}/snapshot/{snap_name}/rollback"
        else:
          logger.warning(f"[{req_id()}] Unsupported VM type for restore: {vtype} ({item})")
          failed += 1
          continue
        logger.info(f"[{req_id()}] Sending restore request path={path}")
        start_flag = 1 if current_status == "running" else 0
        r, error, upid = _run_vm_action(node, vtype, vmid, path, data={"start": start_flag})
        if r == "__unauthorized__":
          return redirect(url_for("session_reset", reason="invalid"))
        if upid:
          jobs.append({"node": node, "upid": upid})
        if error:
          failed += 1
          failure_details.append(f"{node}/{vmid} restore failed ({error})")
          continue
        if r and r.ok:
          done += 1
          success_details.append(f"{node}/{vmid} restore ok")
        else:
          failed += 1
          reason = f"HTTP {r.status_code}" if r is not None else "unknown"
          failure_details.append(f"{node}/{vmid} restore failed ({reason})")
          if r is not None:
            logger.warning(f"[{req_id()}] Restore failed vmid={vmid} node={node} status={r.status_code} body={r.text[:180]!r}")
      elif action == "factory-reset":
        r, error, upid = _direct_reset_vm(node, vtype, vmid, current_status=current_status)
        if r == "__unauthorized__":
          return redirect(url_for("session_reset", reason="invalid"))
        if upid:
          jobs.append({"node": node, "upid": upid})
        if error:
          failed += 1
          failure_details.append(f"{node}/{vmid} reset failed ({error})")
          continue
        if r and r.ok:
          done += 1
          success_details.append(f"{node}/{vmid} reset ok")
        else:
          failed += 1
          status_code = r.status_code if r is not None else "unknown"
          failure_details.append(f"{node}/{vmid} reset failed (HTTP {status_code})")
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
  if jobs:
    session["last_jobs"] = jobs
    jobs_flag = 1
  else:
    session.pop("last_jobs", None)
    jobs_flag = 0
  refresh_flag = 1
  return redirect(url_for("home", bulk=action, done=done, failed=failed, skipped=skipped, fail_list=fail_list, success_list=success_list, skip_list=skip_list, jobs=jobs_flag, refresh=refresh_flag))

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
    raw_vms = [row for row in r.json().get("data", []) if row.get("type") in ("qemu", "lxc") and not row.get("template")]
    
    # Filter by VM.Console permission
    visible_vms = []
    try:
        p = proxmox_get("/access/permissions", cookies=cookies, headers=headers)
        if p.ok:
            perms = p.json().get("data", {})
            for vm in raw_vms:
                vmid = str(vm.get("vmid"))
                if "VM.Console" in perms.get(f"/vms/{vmid}", {}):
                    visible_vms.append(vm)
        else:
            visible_vms = raw_vms
    except:
        visible_vms = raw_vms
        
    # Only fields needed by UI
    slim = [
      {
        "node": row.get("node"),
        "vmid": row.get("vmid"),
        "status": row.get("status"),
        "name": row.get("name"),
        "type": row.get("type"),
      }
      for row in visible_vms
    ]
    
    backend_health = {}
    backend_map = session.get("backend_map") or {}
    if backend_map:
        raw_vms_by_id = {str(vm["vmid"]): vm for vm in raw_vms}
        for sc, b_ids in backend_map.items():
            if b_ids:
                all_running = True
                has_error = False
                for bid in b_ids:
                    b_vm = raw_vms_by_id.get(str(bid))
                    if b_vm:
                        status = b_vm.get("status", "")
                        if status and "error" in status.lower():
                            has_error = True
                            break
                        if status != "running":
                            all_running = False
                            
                if has_error:
                    backend_health[sc] = "ERROR - CONTACT ADMIN"
                elif all_running:
                    backend_health[sc] = "Running"
                else:
                    backend_health[sc] = "unhealthy - reset recommended"
                    
    return jsonify({"vms": slim, "backend_health": backend_health})
  except Exception:
    logger.exception(f"[{req_id()}] /api/vms exception")
    return jsonify({"error": "exception"}), 500

@app.route("/api/vm-notes", methods=["GET"])
@require_session(api=True)
def api_vm_notes():
  node = (request.args.get("node") or "").strip()
  vmid = (request.args.get("vmid") or "").strip()
  vtype = (request.args.get("type") or "").strip().lower()
  if not node or not vmid.isdigit() or vtype not in ("qemu", "lxc"):
    return jsonify({"error": "bad_request"}), 400
  cookies = {"PVEAuthCookie": session.get("pve_ticket")}
  headers = {"CSRFPreventionToken": session.get("pve_csrf")}
  try:
    path = f"/nodes/{node}/{vtype}/{vmid}/config"
    r = proxmox_get(path, cookies=cookies, headers=headers)
    if r.status_code == 401:
      session.clear()
      return jsonify({"error": "unauthorized", "redirect": url_for("session_reset", reason="invalid")}), 401
    if not r.ok:
      return jsonify({"error": "upstream", "status": r.status_code}), 502
    data = r.json().get("data", {})
    notes = data.get("description") or ""
    return jsonify({"notes": notes})
  except Exception:
    logger.exception(f"[{req_id()}] /api/vm-notes exception")
    return jsonify({"error": "exception"}), 500

# Track async task completion for bulk actions
@app.route("/api/jobs", methods=["GET"])
@require_session(api=True)
def api_jobs_status():
  cookies = {"PVEAuthCookie": session.get("pve_ticket")}
  headers = {"CSRFPreventionToken": session.get("pve_csrf")}
  jobs = session.get("last_jobs") or []
  if not jobs:
    return jsonify({"total": 0, "done": 0, "running": 0, "failed": 0, "statuses": []})
  statuses = []
  done = 0
  running = 0
  failed = 0
  for job in jobs:
    node = job.get("node")
    upid = job.get("upid")
    if not node or not upid:
      failed += 1
      statuses.append({"node": node, "upid": upid, "status": "error", "exitstatus": "MISSING"})
      continue
    try:
      r = proxmox_get(f"/nodes/{node}/tasks/{upid}/status", cookies=cookies, headers=headers)
      if r.status_code == 401:
        session.clear()
        return jsonify({"error": "unauthorized", "redirect": url_for("session_reset", reason="invalid")}), 401
      if not r.ok:
        failed += 1
        statuses.append({"node": node, "upid": upid, "status": "error", "exitstatus": f"HTTP {r.status_code}"})
        continue
      payload = r.json().get("data", {})
      status = payload.get("status") or "unknown"
      exitstatus = payload.get("exitstatus")
      statuses.append({"node": node, "upid": upid, "status": status, "exitstatus": exitstatus})
      if status == "stopped":
        done += 1
        if exitstatus and exitstatus != "OK":
          failed += 1
      else:
        running += 1
    except Exception:
      failed += 1
      statuses.append({"node": node, "upid": upid, "status": "error", "exitstatus": "EXCEPTION"})
  total = len(jobs)
  if done >= total:
    session.pop("last_jobs", None)
  return jsonify({"total": total, "done": done, "running": running, "failed": failed, "statuses": statuses})

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
