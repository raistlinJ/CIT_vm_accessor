"""Opt-in browser smoke test against a minimal simulated SPICE guest.

RUN_BROWSER_TESTS=1 python -m pytest tests/test_spice_browser.py -q
Requires playwright and its Chromium browser (or PLAYWRIGHT_CHROMIUM_PATH).
This validates the real vendored protocol client, not real-VM performance.
"""

import os
import queue
import socketserver
import struct
import threading
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import pytest
from werkzeug.serving import make_server

import main
from spice_bridge import SpiceBridge
from test_spice import certificates, response  # shared certificate fixture

pytestmark = pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="opt-in browser test")


def read_exact(stream, size):
    data = b""
    while len(data) < size:
        chunk = stream.recv(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def message(kind, body):
    return struct.pack("<HI", kind, len(body)) + body


@pytest.mark.parametrize("agent_enabled,mouse_mode", [(False, 2), (True, 2), (False, 1)])
def test_browser_renders_guest_accepts_input_and_reconnects(certificates, monkeypatch, tmp_path, agent_enabled, mouse_mode):
    from playwright.sync_api import sync_playwright, expect

    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    keyboard_received = threading.Event()
    pointer_received = threading.Event()
    authenticated_channels = []
    failures = []
    received_clipboards = queue.Queue()
    received_inputs = queue.Queue()
    received_resizes = queue.Queue()
    guest_text = "VM café — こんにちは 😀\n" * 800
    host_text = "Host café — こんにちは 😀\n" * 800
    clipboard_formats = struct.pack("<II", 0, 1)  # CLIPBOARD selection, UTF-8

    def agent_message(kind, payload):
        return struct.pack("<IIQI", 1, kind, 0, len(payload)) + payload

    def send_agent(stream, data):
        # Split even the header, and use >2 KiB text to exercise reassembly.
        chunks = [data[:7], data[7:19]] + [data[i:i + 2048] for i in range(19, len(data), 2048)]
        for chunk in chunks:
            stream.sendall(message(109, chunk))

    class Guest(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                self.request.settimeout(10)
                header = b""
                while not header.endswith(b"\r\n\r\n"):
                    header += read_exact(self.request, 1)
                assert header.startswith(b"CONNECT pvespiceproxy:")
                headers = dict(line.split(b":", 1) for line in header.split(b"\r\n")[1:] if line)
                expected_host = (certificates.config["host"] + ":" + str(certificates.config["tls-port"])).encode()
                if headers.get(b"Host", b"").strip() != expected_host:
                    self.request.sendall(b"HTTP/1.0 401 invalid ticket\r\n\r\n")
                    return
                self.request.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
                with certificates.context.wrap_socket(self.request, server_side=True) as stream:
                    link = read_exact(stream, 16)
                    assert link[:4] == b"REDQ"
                    body = read_exact(stream, struct.unpack_from("<I", link, 12)[0])
                    channel = body[4]
                    reply = struct.pack("<I", 0) + public + struct.pack("<IIII", 1, 0, 178, 11)
                    stream.sendall(b"REDQ" + struct.pack("<III", 2, 2, len(reply)) + reply)
                    authentication = read_exact(stream, 132)
                    password = key.decrypt(authentication[4:], padding.OAEP(
                        mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
                    assert password == certificates.config["password"].encode() + b"\0"
                    authenticated_channels.append(channel)
                    stream.sendall(struct.pack("<I", 0))
                    if channel == 1:  # main init + display/input channel list
                        stream.sendall(message(103, struct.pack("<8I", 42, 1, mouse_mode, mouse_mode, int(agent_enabled), 32, 0, 0)))
                        stream.sendall(message(104, struct.pack("<I4B", 2, 2, 0, 3, 0)))
                        if agent_enabled:
                            # Capabilities + clipboard ownership can share one data stream.
                            send_agent(stream, agent_message(6, struct.pack("<II", 0, 96))
                                       + agent_message(7, clipboard_formats))
                    elif channel == 2:  # primary display surface and solid fill
                        stream.sendall(message(314, struct.pack("<5I", 0, 640, 480, 32, 1)))
                        fill = (struct.pack("<5IB", 0, 0, 0, 480, 640, 0)
                                + struct.pack("<BIH", 1, 0x285078, 8) + bytes(13))
                        stream.sendall(message(302, fill))
                    elif channel == 3:
                        stream.sendall(message(101, struct.pack("<H", 0)))
                    agent_buffer = bytearray()
                    motions = 0
                    while True:
                        kind, size = struct.unpack("<HI", read_exact(stream, 6))
                        payload = read_exact(stream, size)
                        if channel == 3 and kind in (101, 102, 103, 111, 112, 113, 114):
                            # Validate wire bodies, not just the presence of a
                            # message. Relative motion has no display-id byte.
                            assert size == {101: 4, 102: 4, 103: 2, 111: 10, 112: 11, 113: 3, 114: 3}[kind]
                            received_inputs.put((kind, payload))
                            if kind in (111, 112):
                                motions += 1
                                if motions % 4 == 0:
                                    stream.sendall(message(111, b""))
                        if channel == 1 and kind == 107:
                            # Client->guest agent messages are fragmented as well.
                            agent_buffer.extend(payload)
                            stream.sendall(message(110, struct.pack("<I", 1)))
                            while len(agent_buffer) >= 20:
                                _, agent_kind, _, length = struct.unpack_from("<IIQI", agent_buffer)
                                if len(agent_buffer) < 20 + length:
                                    break
                                agent_body = bytes(agent_buffer[20:20 + length])
                                del agent_buffer[:20 + length]
                                if agent_kind == 2:  # monitor config; simulate a guest ignoring resize
                                    count, flags, height, width, depth, x, y = struct.unpack("<7I", agent_body)
                                    assert (count, flags, depth, x, y) == (1, 0, 32, 0, 0)
                                    received_resizes.put((width, height))
                                elif agent_kind == 8:  # client requests guest clipboard
                                    assert agent_body == clipboard_formats
                                    send_agent(stream, agent_message(4, clipboard_formats + guest_text.encode()))
                                elif agent_kind == 7:  # client offers text
                                    assert agent_body == clipboard_formats
                                    send_agent(stream, agent_message(8, clipboard_formats))
                                elif agent_kind == 4:
                                    assert agent_body[:8] == clipboard_formats
                                    received_clipboards.put(agent_body[8:].decode())
                                    # A later guest copy replaces ownership, as in a real desktop.
                                    send_agent(stream, agent_message(7, clipboard_formats))
                        if channel == 3 and kind == 101:
                            keyboard_received.set()
                        if channel == 3 and kind == 113:
                            pointer_received.set()
            except (EOFError, OSError):
                pass
            except Exception as exc:
                failures.append(exc)

    class Proxy(socketserver.ThreadingTCPServer):
        daemon_threads = True

    bridge = SpiceBridge(main.spice_cookie_owner)
    monkeypatch.setattr(main, "spice_bridge", bridge)
    monkeypatch.setattr(main, "proxmox_get", lambda *a, **kw: response([
        {"vmid": 101, "type": "qemu", "node": "node-b", "status": "running"}]))
    monkeypatch.setattr(main, "proxmox_post", lambda *a, **kw: response(certificates.config))
    monkeypatch.setenv("SPICE_PROXY_HOST", "127.0.0.1")
    original_session = main.app.view_functions["spice_session"]

    # In this test there is no Nginx; route the same authenticated browser to
    # the bridge's random local port. Production always uses same-origin WSS.
    def local_session(**kwargs):
        result = original_session(**kwargs)
        if getattr(result, "status_code", None) == 200:
            payload = result.get_json()
            port = bridge.server.sockets[0].getsockname()[1]
            payload["websocket"] = f"http://127.0.0.1:{port}" + payload["websocket"]
            result.set_data(main.app.json.dumps(payload))
        return result

    monkeypatch.setitem(main.app.view_functions, "spice_session", local_session)
    with Proxy(("127.0.0.1", 0), Guest) as proxy:
        monkeypatch.setenv("SPICE_PROXY_PORT", str(proxy.server_address[1]))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        bridge.start(port=0)
        http = make_server("127.0.0.1", 0, main.app, threaded=True)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{http.server_port}"
        cookie = main.app.session_interface.get_signing_serializer(main.app).dumps({
            "pve_ticket": "browser-test-ticket", "pve_csrf": "test-csrf", "pve_login_time": time.time(),
        })
        try:
            with sync_playwright() as playwright:
                browser_type = os.environ.get("PLAYWRIGHT_BROWSER", "chromium")
                browser = getattr(playwright, browser_type).launch(**(
                    {"executable_path": os.environ["PLAYWRIGHT_CHROMIUM_PATH"]}
                    if browser_type == "chromium" and os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") else {}))
                page = browser.new_page(viewport={"width": 1100, "height": 760})
                # Exercise permission outcomes without reading or changing the
                # developer's real system clipboard. Wire transfers remain real.
                page.add_init_script("""(() => {
                  window.clipboardTest = {text: '', reads: 0, writes: [], denied: false};
                  Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
                    readText: async () => {
                      clipboardTest.reads++;
                      if (clipboardTest.pending) return new Promise(resolve => clipboardTest.resolve = resolve);
                      if (clipboardTest.denied) throw new DOMException('Denied', 'NotAllowedError');
                      return clipboardTest.text;
                    },
                    writeText: async text => {
                      if (clipboardTest.denied) throw new DOMException('Denied', 'NotAllowedError');
                      clipboardTest.writes.push(text);
                    }
                  }});
                })();""")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                browser.contexts[0].add_cookies([{"name": "session", "value": cookie, "url": origin}])
                page.goto(origin + "/open?node=node-a&vmid=101&vtype=qemu")
                expect(page.locator("canvas")).to_be_visible(timeout=15000)
                expect(page.locator("#status")).to_contain_text("Connected")
                expect(page.get_by_role("link", name="AccessForge", exact=True)).to_have_count(0)
                pixel = page.locator("canvas").evaluate("c => Array.from(c.getContext('2d').getImageData(10,10,1,1).data)")
                assert pixel == [40, 80, 120, 255]
                page.locator("canvas").click(position={"x": 100, "y": 100})
                page.keyboard.press("a")
                assert keyboard_received.wait(3)
                assert pointer_received.wait(3)
                # Focus can leave while the pointer remains over the canvas
                # (e.g. after using DevTools or tabbing to a toolbar button).
                # Clicking must restore it without requiring a new mouseover.
                page.locator("#clipboard-toggle").evaluate("e => e.focus()")
                page.locator("canvas").click(position={"x": 100, "y": 100})
                expect(page.locator("canvas")).to_be_focused()
                page.keyboard.press("b")
                box = page.locator("canvas").bounding_box()
                page.mouse.move(box["x"] + 140, box["y"] + 120)
                page.mouse.move(box["x"] + 100, box["y"] + 100)
                seen = []
                while not any(kind == 102 and payload == struct.pack("<I", 0xb0) for kind, payload in seen):
                    seen.append(received_inputs.get(timeout=5))
                assert (101, struct.pack("<I", 0x1e)) in seen
                assert (102, struct.pack("<I", 0x9e)) in seen
                assert (101, struct.pack("<I", 0x30)) in seen
                assert (113, struct.pack("<BH", 1, 1)) in seen
                assert (114, struct.pack("<BH", 1, 0)) in seen
                motion_kind = 111 if mouse_mode == 1 else 112
                while not any(kind == motion_kind and struct.unpack_from("<ii", payload) ==
                              ((-40, -20) if mouse_mode == 1 else (100, 100)) for kind, payload in seen):
                    seen.append(received_inputs.get(timeout=5))
                # More than two ACK batches must remain responsive rather
                # than exhausting the client's mouse-motion allowance.
                for step in range(12):
                    page.mouse.move(box["x"] + 101 + step, box["y"] + 101)
                    while True:
                        kind, payload = received_inputs.get(timeout=5)
                        if kind == motion_kind:
                            break
                # Fit remains usable even if the agent is absent or ignores
                # monitor configuration. Check actual pointer coordinates on
                # the transformed surface, not only its visual dimensions.
                channel_count = len(authenticated_channels)
                page.set_viewport_size({"width": 420, "height": 360})
                fitted = """() => {
                  const canvas = document.querySelector('#spice-screen canvas');
                  const bounds = canvas.getBoundingClientRect();
                  const area = document.querySelector('#spice-area').getBoundingClientRect();
                  return bounds.width > 0 && bounds.height > 0 &&
                    bounds.width <= area.width + 1 && bounds.height <= area.height + 1 &&
                    Math.abs(bounds.width / bounds.height - canvas.width / canvas.height) < .01;
                }"""
                page.wait_for_function(fitted)
                scaled = page.locator("canvas").bounding_box()
                assert scaled["width"] < 640
                page.mouse.click(scaled["x"] + scaled["width"] * .6,
                                 scaled["y"] + scaled["height"] * .6)
                if mouse_mode == 2:
                    positions = []
                    while True:
                        try:
                            kind, payload = received_inputs.get(timeout=5)
                        except queue.Empty:
                            pytest.fail(f"Scaled pointer positions {positions}; canvas bounds {scaled}")
                        if kind == 112:
                            x, y = struct.unpack_from("<II", payload)
                            positions.append((x, y))
                            # MouseEvent client coordinates are rounded to
                            # CSS pixels; one CSS pixel spans several guest
                            # pixels when the popup is this small.
                            if (abs(x / 640 - .6) * scaled["width"] <= 1
                                    and abs(y / 480 - .6) * scaled["height"] <= 1):
                                break
                expected_size = tuple(page.locator("#spice-area").evaluate("""e => [
                  Math.max(320, Math.floor(e.clientWidth / 8) * 8),
                  Math.max(200, Math.floor(e.clientHeight / 8) * 8)
                ]"""))
                if agent_enabled:
                    while received_resizes.get(timeout=5) != expected_size:
                        pass
                    while not received_resizes.empty():
                        received_resizes.get_nowait()
                page.locator("#spice-screen").evaluate("e => e.style.transform = 'scale(1)'")
                page.get_by_role("button", name="Fit to window", exact=True).click()
                page.wait_for_function(fitted)
                if agent_enabled:
                    # Explicit fit must resend even when dimensions did not
                    # change; automatic resize deduplication must not eat it.
                    assert received_resizes.get(timeout=5) == expected_size
                else:
                    assert received_resizes.empty()
                assert len(authenticated_channels) == channel_count
                page.screenshot(path=str(tmp_path / "spice-small-window.png"))
                page.set_viewport_size({"width": 1100, "height": 760})
                page.wait_for_function("() => document.querySelector('canvas').getBoundingClientRect().width === 640")
                page.get_by_role("button", name="Clipboard", exact=True).click()
                page.wait_for_function(fitted)
                if agent_enabled:
                    expect(page.locator("#clipboard-send-button")).to_be_enabled()
                    expect(page.locator("#clipboard-receive")).to_have_value(guest_text)
                    # Focusing the VM must not silently read or overwrite OS clipboard.
                    assert page.evaluate("clipboardTest.reads") == 0
                    assert page.evaluate("clipboardTest.writes") == []
                    page.get_by_role("button", name="Copy from VM", exact=True).click()
                    assert page.evaluate("clipboardTest.writes") == [guest_text]
                    page.evaluate("text => clipboardTest.text = text", host_text)
                    page.get_by_role("button", name="Paste from computer", exact=True).click()
                    expect(page.locator("#clipboard-send")).to_have_value(host_text)
                    page.get_by_role("button", name="Send to VM", exact=True).click()
                    assert received_clipboards.get(timeout=5) == host_text

                    # Denied API access still allows manual paste/copy via text areas.
                    page.evaluate("clipboardTest.denied = true")
                    page.get_by_role("button", name="Paste from computer", exact=True).click()
                    expect(page.locator("#clipboard-status")).to_contain_text("Paste into the text box")
                    page.locator("#clipboard-send").fill("manual paste\nUnicode ✓")
                    page.get_by_role("button", name="Send to VM", exact=True).click()
                    assert received_clipboards.get(timeout=5) == "manual paste\nUnicode ✓"
                    expect(page.locator("#clipboard-receive")).to_have_value(guest_text)
                    page.get_by_role("button", name="Copy from VM", exact=True).click()
                    expect(page.locator("#clipboard-status")).to_contain_text("VM text is selected")
                    assert page.locator("#clipboard-receive").evaluate("e => e.selectionEnd - e.selectionStart") == len(guest_text.encode('utf-16-le')) // 2

                    # Empty text is a valid clipboard update, not a missing value.
                    page.locator("#clipboard-send").fill("")
                    page.get_by_role("button", name="Send to VM", exact=True).click()
                    assert received_clipboards.get(timeout=5) == ""
                    page.locator("#clipboard-send").fill("é" * (512 * 1024 + 1))
                    page.get_by_role("button", name="Send to VM", exact=True).click()
                    expect(page.locator("#clipboard-status")).to_contain_text("limited to 1 MiB")
                    page.locator("#clipboard-send").fill("draft cleared on disconnect")
                else:
                    expect(page.locator("#clipboard-send-button")).to_be_disabled()
                    expect(page.locator("#clipboard-copy")).to_be_disabled()
                    expect(page.locator("#clipboard-status")).to_contain_text("SPICE guest agent")
                page.screenshot(path=str(tmp_path / "spice-connected.png"))
                if agent_enabled:
                    page.evaluate("clipboardTest.pending = true")
                    page.get_by_role("button", name="Paste from computer", exact=True).click()
                page.get_by_role("button", name="Disconnect", exact=True).click()
                expect(page.locator("#status")).to_have_text("Disconnected.")
                expect(page.locator("canvas")).to_have_count(0)
                expect(page.get_by_role("button", name="Fit to window", exact=True)).to_be_disabled()
                expect(page.locator("#clipboard-send")).to_have_value("")
                expect(page.locator("#clipboard-receive")).to_have_value("")
                expect(page.locator("#clipboard-copy")).to_be_disabled()
                if agent_enabled:
                    page.evaluate("clipboardTest.resolve('late clipboard value')")
                    expect(page.locator("#clipboard-send")).to_have_value("")
                page.get_by_role("button", name="Reconnect", exact=True).click()
                expect(page.locator("canvas")).to_be_visible(timeout=15000)
                assert len(authenticated_channels) >= 6
                page.get_by_role("button", name="Ctrl+Alt+Del", exact=True).click()
                expect(page.get_by_role("link", name="Use noVNC")).to_be_visible()
                assert not errors
                assert not failures
                browser.close()
        finally:
            http.shutdown()
            bridge.stop()
            proxy.shutdown()
