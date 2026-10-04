"""Inspect client SPICE frames before forwarding guest-agent file traffic.

Only the browser client's mini-header / SPICE-ticket dialect is accepted.
TCP, WebSocket, SPICE, and guest-agent packet boundaries need not align.
"""
import struct


class SpicePolicyError(ValueError):
    pass


class SpiceClientInspector:
    def __init__(self):
        self.buffer = bytearray()
        self.stage = "link"
        self.channel = None
        self.agent_header = bytearray()
        self.agent_remaining = 0
        self.agent_type = None

    def feed(self, data):
        self.buffer.extend(data)
        output = []
        while True:
            if self.stage == "link":
                if len(self.buffer) < 16:
                    break
                magic, major, minor, size = struct.unpack_from("<4sIII", self.buffer)
                if magic != b"REDQ" or major != 2 or size < 18 or size > 4096:
                    raise SpicePolicyError("Unsupported SPICE link")
                if len(self.buffer) < 16 + size:
                    break
                body = self.buffer[16:16 + size]
                self.channel = body[4]
                common, channels, offset = struct.unpack_from("<III", body, 6)
                if (self.channel not in range(1, 7) or common < 1 or offset < 18
                        or offset + 4 * (common + channels) != size):
                    raise SpicePolicyError("Unsupported SPICE channel or capabilities")
                caps = struct.unpack_from("<I", body, offset)[0]
                if caps & 9 != 9:  # authentication selection + mini headers
                    raise SpicePolicyError("Mini headers and ticket authentication required")
                output.append((self._take(16 + size), False, False))
                self.stage = "ticket"
            elif self.stage == "ticket":
                if len(self.buffer) < 132:
                    break
                if struct.unpack_from("<I", self.buffer)[0] != 1:
                    raise SpicePolicyError("Unsupported authentication")
                output.append((self._take(132), False, False))
                self.stage = "messages"
            else:
                if len(self.buffer) < 6:
                    break
                kind, size = struct.unpack_from("<HI", self.buffer)
                if size > 1024 * 1024:
                    raise SpicePolicyError("SPICE client message too large")
                if len(self.buffer) < 6 + size:
                    break
                frame = self._take(6 + size)
                upload = start = False
                if self.channel == 1:
                    # Deny migration/tunnel extensions that could bypass inspection.
                    if kind not in {1, 2, 3, 4, 5, 6, 104, 105, 106, 107, 108}:
                        raise SpicePolicyError("Unsupported main-channel message")
                    if kind == 106 and (self.agent_header or self.agent_remaining):
                        raise SpicePolicyError("Agent restart inside a message")
                    if kind == 107:
                        upload, start = self._agent(frame[6:])
                output.append((frame, upload, start))
        return output

    def _take(self, count):
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def _agent(self, data):
        upload = start = False
        cursor = 0
        while cursor < len(data):
            if self.agent_remaining:
                upload |= self.agent_type in {10, 11, 12}
                count = min(self.agent_remaining, len(data) - cursor)
                cursor += count
                self.agent_remaining -= count
                continue
            count = min(20 - len(self.agent_header), len(data) - cursor)
            self.agent_header.extend(data[cursor:cursor + count])
            cursor += count
            if len(self.agent_header) == 20:
                protocol, kind, opaque, size = struct.unpack("<IIQI", self.agent_header)
                self.agent_header.clear()
                if protocol != 1 or kind not in {*range(1, 13), 14} or size > 2 * 1024 * 1024:
                    raise SpicePolicyError("Unsupported guest-agent message")
                self.agent_type = kind
                self.agent_remaining = size
                upload |= kind in {10, 11, 12}
                start |= kind == 10
        return upload, start
