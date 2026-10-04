"""Browser WebSocket transport for Proxmox's CONNECT + TLS SPICE proxy.

Runs beside Waitress in the same process. Only server-issued, short-lived,
session-bound grants can open tunnels; clients cannot choose a TCP destination.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass, field
import errno
import hashlib
from http import HTTPStatus
import logging
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from spice_policy import SpiceClientInspector, SpicePolicyError

logger = logging.getLogger(__name__)
# WebSocket debug logging includes cookies and grant URLs.
wire_logger = logging.getLogger("accessforge.spice.transport")
wire_logger.setLevel(logging.WARNING)


def _loopback_socketpair():
    """Create the event loop's internal wakeup pair without AF_UNIX.

    Some container policies deny Unix socket pairs while permitting TCP. The
    temporary listener binds only to loopback and closes before this returns.
    This connection carries wakeup bytes, not SPICE traffic or credentials.
    """
    client = reader = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.settimeout(3)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(3)
            client.connect(listener.getsockname())
            reader, peer = listener.accept()
            if peer != client.getsockname():
                raise OSError("Unexpected peer on internal event-loop connection")
            for endpoint in (reader, client):
                endpoint.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return reader, client
    except BaseException:
        for endpoint in (reader, client):
            if endpoint is not None:
                endpoint.close()
        raise


class _BridgeSelectorLoop(asyncio.SelectorEventLoop):
    """Selector loop with a pre-created wakeup pair, scoped to this bridge.

    _make_self_pipe is CPython's selector-loop hook (Python 3.11–3.14). Keeping
    this override local avoids monkey-patching socket.socketpair process-wide.
    """
    def __init__(self, wakeup_pair):
        self._wakeup_pair = wakeup_pair
        super().__init__()

    def _make_self_pipe(self):
        self._ssock, self._csock = self._wakeup_pair
        self._ssock.setblocking(False)
        self._csock.setblocking(False)
        self._internal_fds += 1
        self._add_reader(self._ssock.fileno(), self._read_from_self)


def _new_bridge_loop():
    # Create sockets before constructing the loop so a denied fallback cannot
    # leave a half-initialized event loop with a failing __del__ method.
    try:
        pair = socket.socketpair()
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM, errno.EAFNOSUPPORT, errno.EOPNOTSUPP):
            raise
        logger.warning("Native socketpair unavailable; using loopback TCP for SPICE event-loop wakeups")
        pair = _loopback_socketpair()
    try:
        return _BridgeSelectorLoop(pair)
    except BaseException:
        for endpoint in pair:
            endpoint.close()
        raise


def session_owner(ticket):
    return hashlib.sha256(ticket.encode()).hexdigest()


@dataclass(repr=False)
class Target:
    proxy_host: str
    proxy_port: int
    authority: str
    context: ssl.SSLContext
    subject: x509.Name

    @classmethod
    def from_config(cls, config, proxy_host, proxy_port=3128):
        # Proxmox's "host" is a signed routing ticket, not a DNS hostname.
        host = config.get("host", "")
        port = int(config.get("tls-port", 0))
        if (config.get("type") != "spice" or not isinstance(host, str)
                or not re.fullmatch(r"pvespiceproxy:[A-Za-z0-9:+/=._@-]+", host)
                or not 1 <= port <= 65535 or not config.get("password")):
            raise ValueError("Invalid Proxmox SPICE configuration")
        # Trust only the cluster CA supplied by the authenticated API. Proxmox
        # explicitly provides host-subject because its routing ticket isn't a
        # valid TLS hostname. Never disable certificate-chain verification.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cadata=config["ca"].replace("\\n", "\n"))
        subject = x509.Name.from_rfc4514_string(config["host-subject"])
        if not subject:
            raise ValueError("Missing SPICE certificate subject")
        if not proxy_host or not 1 <= int(proxy_port) <= 65535:
            raise ValueError("Invalid SPICE proxy address")
        return cls(proxy_host, int(proxy_port), f"{host}:{port}", context, subject)


def check_subject(der_certificate, expected):
    actual = x509.load_der_x509_certificate(der_certificate).subject
    # Proxmox serializes subject attributes in OpenSSL's display order.
    attributes = lambda name: Counter((a.oid.dotted_string, a.value) for a in name)
    if attributes(actual) != attributes(expected):
        raise ssl.SSLCertVerificationError("SPICE certificate subject mismatch")


async def open_tunnel(target):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(target.proxy_host, target.proxy_port, limit=16384), 10
    )
    try:
        # Proxmox authenticates the signed routing ticket from Host, not the
        # request line (PVE/APIServer/AnyEvent.pm). Both must carry the full
        # ticket + TLS port, including when the proxy routes to another node.
        writer.write((f"CONNECT {target.authority} HTTP/1.0\r\n"
                      f"Host: {target.authority}\r\n\r\n").encode("ascii"))
        await writer.drain()
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        status = header.split(b"\r\n", 1)[0].split()
        if len(status) < 2 or status[0] not in (b"HTTP/1.0", b"HTTP/1.1") or status[1] != b"200":
            # Never log the response body/reason: it can echo the routing ticket.
            code = status[1].decode("ascii") if len(status) > 1 and re.fullmatch(rb"[1-5][0-9]{2}", status[1]) else "invalid response"
            logger.warning("Proxmox SPICE CONNECT rejected (HTTP %s)", code)
            raise ConnectionError("Proxmox SPICE proxy rejected the tunnel")
        await writer.start_tls(target.context, server_hostname="", ssl_handshake_timeout=10)
        check_subject(writer.get_extra_info("ssl_object").getpeercert(binary_form=True), target.subject)
        return reader, writer
    except BaseException:
        writer.close()
        raise


@dataclass(repr=False)
class Grant:
    target: Target
    owner: str
    origin: str
    connect_until: float
    expires: float
    connections: set = field(default_factory=set)
    upload_allowed: bool = False
    upload_check: object = None


class SpiceBridge:
    def __init__(self, cookie_owner, max_sessions=128, max_per_user=8, max_channels=16):
        self.cookie_owner = cookie_owner
        self.max_sessions = max_sessions
        self.max_per_user = max_per_user
        self.max_channels = max_channels
        self.grants = {}
        self.lock = threading.Lock()
        self.loop = None
        self.thread = None
        self.server = None

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive() and self.server is not None

    def issue(self, target, owner, origin, lifetime, *, upload_allowed=False, upload_check=None):
        now = time.monotonic()
        with self.lock:
            self._prune(now)
            if (len(self.grants) >= self.max_sessions or
                    sum(g.owner == owner for g in self.grants.values()) >= self.max_per_user):
                raise RuntimeError("Too many open consoles. Close a console and try again.")
            token = secrets.token_urlsafe(32)
            # QEMU expires newly issued SPICE passwords after 30 seconds.
            self.grants[token] = Grant(target, owner, origin, now + 25, now + lifetime,
                                       upload_allowed=upload_allowed, upload_check=upload_check)
        return token

    def _prune(self, now):
        for token, grant in list(self.grants.items()):
            if not grant.connections and now >= min(grant.connect_until, grant.expires):
                del self.grants[token]

    def _lookup(self, request):
        parsed = urlsplit(request.path)
        if parsed.path != "/spice/ws":
            return None
        token = parse_qs(parsed.query).get("token", [""])[0]
        owner = self.cookie_owner(request.headers.get("Cookie", ""))
        grant = self.grants.get(token)
        now = time.monotonic()
        if (not grant or not owner or owner != grant.owner or
                request.headers.get("Origin") != grant.origin or
                now >= min(grant.connect_until, grant.expires) or
                len(grant.connections) >= self.max_channels):
            return None
        return grant

    def authorize(self, connection, request):
        with self.lock:
            grant = self._lookup(request)
        if grant is None:
            return connection.respond(HTTPStatus.FORBIDDEN, "Console session unavailable. Reconnect from AccessForge.\n")

    async def relay(self, websocket):
        with self.lock:
            grant = self._lookup(websocket.request)
            if grant:
                grant.connections.add(websocket)
        if grant is None:
            await websocket.close(1008, "Console session unavailable")
            return
        writer = None
        tasks = []
        try:
            reader, writer = await open_tunnel(grant.target)

            async def to_browser():
                while data := await reader.read(65536):
                    await websocket.send(data)

            async def to_vm():
                inspector = SpiceClientInspector()
                checked_at = -float("inf")
                async for data in websocket:
                    if not isinstance(data, bytes):
                        await websocket.close(1003, "Binary SPICE frames required")
                        return
                    for frame, upload, start in inspector.feed(data):
                        if upload:
                            if not grant.upload_allowed:
                                raise SpicePolicyError("VM file uploads are disabled")
                            if start or time.monotonic() - checked_at >= 1:
                                if grant.upload_check is None or not await asyncio.to_thread(grant.upload_check):
                                    raise SpicePolicyError("VM file uploads are disabled")
                                checked_at = time.monotonic()
                        writer.write(frame)
                        await writer.drain()

            tasks = [asyncio.create_task(to_browser()), asyncio.create_task(to_vm())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except ConnectionClosed:
            pass
        except SpicePolicyError:
            await websocket.close(1008, "SPICE traffic rejected by VM transfer policy. Reconnect the console.")
        except Exception as exc:
            # Exception messages may contain a signed proxy ticket. Log types only.
            logger.warning("SPICE tunnel failed (%s)", type(exc).__name__)
            await websocket.close(1011, "SPICE connection failed; check proxy connectivity and VM configuration")
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if writer:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 3)
                except (Exception, asyncio.CancelledError):
                    pass
            with self.lock:
                grant.connections.discard(websocket)
                if not grant.connections:
                    for token, candidate in list(self.grants.items()):
                        if candidate is grant:
                            del self.grants[token]

    def revoke(self, owner):
        connections = []
        with self.lock:
            for token, grant in list(self.grants.items()):
                if grant.owner == owner:
                    connections.extend(grant.connections)
                    del self.grants[token]
        if self.loop and self.loop.is_running():
            for connection in connections:
                asyncio.run_coroutine_threadsafe(connection.close(1008, "Signed out"), self.loop)

    async def _reap(self):
        while True:
            await asyncio.sleep(5)
            connections = []
            with self.lock:
                now = time.monotonic()
                self._prune(now)
                for token, grant in list(self.grants.items()):
                    if now >= grant.expires:
                        connections.extend(grant.connections)
                        del self.grants[token]
            for connection in connections:
                await connection.close(1008, "Session expired; sign in again")

    def start(self, host="127.0.0.1", port=8081):
        ready = threading.Event()
        errors = []
        stage = "initialize its event loop"

        async def run():
            nonlocal stage
            self.loop = asyncio.get_running_loop()
            self.stop_event = asyncio.Event()
            stage = f"bind its WebSocket listener on {host}:{port}"
            async with serve(
                self.relay, host, port, subprotocols=["binary"],
                process_request=self.authorize, compression=None,
                max_size=1024 * 1024, max_queue=16, close_timeout=3,
                logger=wire_logger,
            ) as server:
                self.server = server
                ready.set()
                reaper = asyncio.create_task(self._reap())
                try:
                    await self.stop_event.wait()
                finally:
                    reaper.cancel()
                    await asyncio.gather(reaper, return_exceptions=True)

        def worker():
            try:
                # Enter Runner before creating the coroutine. If event-loop
                # initialization fails, there is no unawaited coroutine to leak.
                with asyncio.Runner(loop_factory=_new_bridge_loop) as runner:
                    runner.run(run())
            except Exception as exc:
                errors.append(exc)
                ready.set()
            finally:
                self.server = None

        self.thread = threading.Thread(target=worker, name="spice-bridge", daemon=True)
        self.thread.start()
        if not ready.wait(10):
            raise RuntimeError("SPICE bridge did not start")
        if errors:
            raise RuntimeError(f"SPICE bridge could not {stage}: {errors[0]}") from errors[0]

    def stop(self):
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.stop_event.set)
            self.thread.join(timeout=10)
