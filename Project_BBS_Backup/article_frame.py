"""Track synchronized terminal redraws without waiting or sending more keys."""
import re

BEGIN = b'\x1b[?2026h'
END = b'\x1b[?2026l'
MARKERS = re.compile(rb'\x1b\[\?2026([hl])')


class ArticleFrame:
    def __init__(self):
        self.active = False
        self.pending = b''
        self.starts = self.ends = self.blocked = 0

    def feed(self, chunk):
        data = self.pending + chunk
        self.pending = b''
        for match in MARKERS.finditer(data):
            self.active = match[1] == b'h'
            if self.active:
                self.starts += 1
            else:
                self.ends += 1
        # A marker itself can be divided between WebSocket messages.
        for size in range(min(len(BEGIN)-1, len(data)), 0, -1):
            if BEGIN.startswith(data[-size:]) or END.startswith(data[-size:]):
                self.pending = data[-size:]
                break

    def ready(self):
        accepted = not self.active and not self.pending
        if not accepted:
            self.blocked += 1
        return accepted

    def diagnostics(self):
        return {'starts': self.starts, 'ends': self.ends, 'in_progress': self.active,
                'marker_pending': bool(self.pending), 'blocked_checks': self.blocked}


class BlankScreenControls:
    """Remove only a reverse-index that cannot change a freshly cleared screen.

    PyPtt does not implement ESC M. Following 2J and cursor-only commands,
    at the top margin before any cell is written, it has no visible effect. All other reverse
    indices remain untouched; this is not a general scrolling emulator.
    """
    def __init__(self):
        self.pending = b''
        self.blank = False
        self.row = None
        self.removed = 0
        self.unhandled = 0

    def reset_frame(self):
        self.pending = b''
        self.blank = False
        self.row = None

    def feed(self, chunk):
        data = self.pending + chunk
        self.pending = b''
        output = bytearray()
        i = 0
        while i < len(data):
            if data[i] != 27:
                if data[i] == 10 and self.row is not None: self.row += 1
                elif data[i] != 13:
                    self.blank = False
                    self.row = None
                output.append(data[i]); i += 1
                continue
            if i+1 == len(data):
                self.pending = data[i:]; break
            if data[i+1] == 91:
                end = i+2
                while end < len(data) and 32 <= data[end] <= 63 and end-i < 64: end += 1
                if end == len(data) and end-i < 64:
                    self.pending = data[i:]; break
                if end < len(data) and 64 <= data[end] <= 126:
                    token = data[i:end+1]
                    if token == b'\x1b[2J': self.blank = True
                    elif token[-1:] in (b'H', b'f'):
                        try: self.row = max(0, int(token[2:-1].split(b';')[0] or b'1')-1)
                        except ValueError: self.row = None
                    elif not (token[-1:] in (b'H',b'f',b'J',b'K',b'm') or token in (BEGIN,END)):
                        self.blank = False
                        self.row = None
                    output.extend(token); i = end+1
                    continue
            if data[i:i+2] == b'\x1bM':
                if self.blank and self.row == 0: self.removed += 1
                else:
                    self.unhandled += 1
                    self.blank = False
                    self.row = None
                    output.extend(data[i:i+2])
            else:
                self.blank = False
                output.extend(data[i:i+2])
            i += 2
        return bytes(output)
