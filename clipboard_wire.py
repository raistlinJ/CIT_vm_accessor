"""Gate complete SPICE clipboard messages before forwarding any text bytes.

Credits for buffered agent packets prevent a large clipboard from exhausting the
sender's token window. Matching later recipient credits are consumed locally.
"""
import struct
from spice_policy import SpicePolicyError

MAX_CLIPBOARD_BYTES = 1024 * 1024


def packet(kind, body):
    return struct.pack('<HI', kind, len(body)) + body


class ClipboardWire:
    def __init__(self, server=False):
        self.server = server
        self.stage = 'link'
        self.buffer = bytearray()
        self.agent = bytearray()
        self.frames = []
        self.events = []
        self.credited = 0
        self.debt = 0
        self.caps = 0
        self.peer = None
        self.requested = False
        self.request_peer = False
        self.discard_remaining = 0

    def feed(self, data):
        self.buffer.extend(data)
        output = []
        while True:
            if self.stage == 'link':
                if len(self.buffer) < 16:
                    break
                magic, major, minor, size = struct.unpack_from('<4sIII', self.buffer)
                if magic != b'REDQ' or major != 2 or size > 4096:
                    raise SpicePolicyError('Invalid SPICE handshake')
                if len(self.buffer) < size+16:
                    break
                output.append((self._take(size+16), None))
                self.stage = 'auth'
            elif self.stage == 'auth':
                size = 4 if self.server else 132
                if len(self.buffer) < size:
                    break
                output.append((self._take(size), None))
                self.stage = 'messages'
            else:
                if len(self.buffer) < 6:
                    break
                kind, size = struct.unpack_from('<HI', self.buffer)
                if size > 16*1024*1024:
                    raise SpicePolicyError('Oversized SPICE message')
                if len(self.buffer) < 6+size:
                    break
                frame = self._take(size+6)
                output.append((frame, kind))
        return output

    def _take(self, size):
        frame = bytes(self.buffer[:size])
        del self.buffer[:size]
        return frame

    def prepare(self, frame, kind):
        """Return (forward frames, sender credit, complete batch events or None)."""
        token_kind = 110 if self.server else 108
        data_kind = 109 if self.server else 107
        if kind == token_kind:
            if len(frame) != 10:
                raise SpicePolicyError('Invalid agent token count')
            tokens = struct.unpack_from('<I', frame, 6)[0]
            consumed = min(tokens, self.peer.debt)
            self.peer.debt -= consumed
            tokens -= consumed
            return ([packet(kind, struct.pack('<I', tokens))] if tokens else []), None, None
        if kind != data_kind:
            if self.server and kind in (107, 108, 115):  # Agent reconnect or disconnect.
                self.discard_remaining = 0
            if self.agent and kind == (107 if self.server else 106):
                raise SpicePolicyError('Agent restarted during clipboard transfer')
            return [frame], None, None
        if self.discard_remaining:
            discarded = min(self.discard_remaining, len(frame) - 6)
            self.discard_remaining -= discarded
            if discarded == len(frame) - 6:
                return [], packet(108, struct.pack('<I', 1)), None
            # The last chunk may also contain the start of another message.
            frame = packet(kind, frame[6 + discarded:])
        self.frames.append(frame)
        self.agent.extend(frame[6:])
        if sum(map(len, self.frames)) > 3*1024*1024:
            raise SpicePolicyError('Oversized buffered agent message')
        while len(self.agent) >= 20:
            protocol, agent_kind, opaque, size = struct.unpack_from('<IIQI', self.agent)
            if protocol != 1:
                raise SpicePolicyError('Invalid agent message')
            if size > 2*1024*1024:
                if not self.server or agent_kind != 4:
                    raise SpicePolicyError('Invalid agent message')
                # Drain a large guest clipboard response without retaining its
                # payload. Reimburse dropped frames to free the sender's tokens.
                discarded = min(size, len(self.agent) - 20)
                tail = bytes(self.agent[20 + discarded:])
                self.agent.clear()
                self.discard_remaining = size - discarded
                self.requested = False
                frames, credit = self.finish(False)
                if tail:
                    return self.prepare(packet(kind, tail), kind)
                return frames, credit, None
            if len(self.agent) < 20+size:
                break
            body = bytes(self.agent[20:20+size])
            del self.agent[:20+size]
            if agent_kind == 6:
                if len(body) < 8:
                    raise SpicePolicyError('Invalid agent capabilities')
                self.caps = struct.unpack_from('<I', body, 4)[0]
            elif agent_kind in (4, 7, 8, 9):
                # The selection header exists only when both peers support it.
                prefix = 4 if self.caps & self.peer.caps & 64 else 0
                direction = 'from_vm' if self.server else 'to_vm'
                if agent_kind == 8:
                    direction = 'to_vm' if self.server else 'from_vm'
                if len(body) < prefix:
                    self.events.append((direction, -1))
                    continue
                if prefix and body[:4] != bytes(4):
                    # PRIMARY/mouse selections must not consume an allowance.
                    self.events.append((direction, -1))
                    continue
                if agent_kind == 8:
                    if len(body) != prefix + 4:
                        self.events.append((direction, -1))
                        continue
                    self.request_peer = True
                if agent_kind == 7 and (len(body) - prefix) % 4:
                    self.events.append((direction, -1))
                    continue
                count = 0
                if agent_kind == 4:
                    if self.server and not self.requested:
                        self.events.append((direction, -1))
                        continue
                    self.requested = False
                    if len(body) < prefix+4:
                        self.events.append((direction, -1))
                        continue
                    text_type = struct.unpack_from('<I', body, prefix)[0]
                    if text_type not in (0, 1):
                        # Binary formats have no meaningful character allowance.
                        self.events.append((direction, -1))
                        continue
                    if text_type == 1:
                        text = body[prefix+4:]
                        if len(text) > MAX_CLIPBOARD_BYTES:
                            self.events.append((direction, -1))
                            continue
                        try:
                            count = len(text.decode('utf-8'))
                        except UnicodeDecodeError:
                            # The agent stream is still framed correctly. Deny
                            # this transfer without closing the console.
                            self.events.append((direction, -1))
                            continue
                self.events.append((direction, count))
        if self.agent:
            self.credited += 1
            credit = packet(108 if self.server else 110, struct.pack('<I', 1))
            return [], credit, None
        return [], None, self.events

    def finish(self, allowed):
        frames = self.frames if allowed else []
        credit = None
        if allowed:
            self.debt += self.credited
            if self.request_peer:
                self.peer.requested = True
        else:
            # Dropped packets will never produce recipient credits. Reimburse
            # the final packet and retire debts for the earlier buffered ones.
            credit = packet(108 if self.server else 110, struct.pack('<I', len(self.frames)-self.credited))
        self.frames, self.events, self.credited, self.request_peer = [], [], 0, False
        return frames, credit
