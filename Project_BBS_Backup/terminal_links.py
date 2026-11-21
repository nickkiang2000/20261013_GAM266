"""Strip OSC 8 hyperlink commands before they reach the terminal renderer."""

import re


OSC8 = b'\x1b]8;'
TERMINATOR = re.compile(rb'\x07|\x1b\\')
SGR_BYTES = re.compile(rb'\x1b\[[0-9;]*m')
WEB_URL = re.compile(r"""https?://[^\s<>"'\x00-\x1f\x7f]+""", re.I)
MAX_COMMAND_BYTES = 8192


class TerminalLinks:
    def __init__(self):
        self.pending = b''
        self.target = ''
        self.label = bytearray()
        self.links = {}
        self.targets = set()
        self.commands = 0

    def _visible(self, data):
        if self.target and len(self.label) <= MAX_COMMAND_BYTES:
            self.label.extend(data[:MAX_COMMAND_BYTES + 1 - len(self.label)])
        return data

    def feed(self, chunk):
        if b'\x1b' not in chunk and not self.pending:
            return self._visible(chunk)
        data = self.pending + chunk
        self.pending = b''
        parts = []
        while data:
            start = data.find(OSC8)
            if start < 0:
                # An escape prefix can be divided across WebSocket chunks.
                size = min(len(OSC8) - 1, len(data))
                while size and not OSC8.startswith(data[-size:]):
                    size -= 1
                if size:
                    self.pending = data[-size:]
                    data = data[:-size]
                parts.append(self._visible(data))
                break
            parts.append(self._visible(data[:start]))
            end = TERMINATOR.search(data, start + len(OSC8))
            if end is None or end.start() - start > MAX_COMMAND_BYTES:
                if len(data) - start <= MAX_COMMAND_BYTES:
                    self.pending = data[start:]
                else:
                    # Preserve the original failure on malformed commands.
                    parts.append(data[start:])
                break
            command = data[start + len(OSC8):end.start()]
            _, separator, target = command.partition(b';')
            if not separator or any(byte < 32 or byte > 126 for byte in command):
                parts.append(self._visible(data[start:end.end()]))
            else:
                self.commands += 1
                label = SGR_BYTES.sub(b'', self.label).decode('ascii', errors='replace')
                if self.target and WEB_URL.fullmatch(label) and label.isascii():
                    self.links.setdefault(label, set()).add(self.target)
                address = target.decode('ascii', errors='replace')
                self.target = address if address.isascii() and WEB_URL.fullmatch(address) else ''
                if self.target:
                    self.targets.add(self.target)
                self.label.clear()
            data = data[end.end():]
        return b''.join(parts)
