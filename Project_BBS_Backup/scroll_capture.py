"""Join scrolling screens by visible overlap, independently of footer line offsets."""
from collections import deque
from PyPtt import screens


def padding(row):
    text, runs = row
    return not text.strip() and not any(token.startswith('b') for _, style in runs for token in style)


def wrap_prefix(row):
    text, runs = row
    last = text.rstrip(' ')
    if not last.endswith('\\') or screens._cell_len(last.replace('\x08', '')) not in (78, 79, 80):
        return None
    glyph = len(last) - 1
    prefix = []
    offset = 0
    for value, style in runs:
        if offset <= glyph < offset + len(value):
            if set(style) != {'f7', 'hl'}:
                return None
            if glyph > offset:
                prefix.append((value[:glyph-offset], style))
            return last[:-1], tuple(prefix)
        prefix.append((value, style))
        offset += len(value)
    return None


def overlap_candidates(previous, current):
    """Require matching text AND color, with at least two distinct text anchors."""
    return [size for size in range(2, min(len(previous), len(current)) + 1)
            if previous[-size:] == current[:size]
            and len({text for text, _ in current[:size] if text.strip()}) >= 2]


class ScrollCapture:
    def __init__(self):
        self.rows = []
        self.previous = None
        self.last_range = None
        self.end = None
        self.waiting = 0
        self.moves = deque(maxlen=12)
        self.initial_extra = 0
        self.result = None
        self.error = None
        self.revision = 0

    @staticmethod
    def fold(rows):
        result = []
        text, runs = '', ()
        continuing = False
        for row in rows:
            marker = wrap_prefix(row)
            if marker is not None:
                text += marker[0]
                runs += marker[1]
                continuing = True
            else:
                result.append((text + row[0], runs + row[1]))
                text, runs = '', ()
                continuing = False
        return result, continuing

    def observe(self, styled, first, last, at_end):
        self.result = None
        if styled is None:
            self.waiting += 1
            self.error = 'colors_unavailable'
            return
        current = [(str(row), tuple((text, tuple(style)) for text, style in row.color_runs)) for row in styled]
        if self.previous is None:
            if first != 1:
                self.error = 'missing_start'
                self.waiting += 1
                return
            self.rows = current[:]
            self.previous = current
            self.last_range = (first, last)
            folded, pending = self.fold(current)
            # A first screen can expose one extra source line. A blank final
            # cell alone cannot establish that offset.
            self.initial_extra = int(len(folded) + int(pending) == last + 1 and not padding(current[-1]))
            self.revision += 1
        elif current == self.previous and (first, last) == self.last_range:
            pass
        else:
            candidates = overlap_candidates(self.previous, current)
            self.moves.append({'reported': [first, last], 'overlaps': candidates})
            if len(candidates) != 1 or (self.last_range and first < self.last_range[0]):
                self.waiting += 1
                self.error = 'overlap_ambiguous' if candidates else 'overlap_missing'
                return
            overlap = candidates[0]
            # A fully identical window with advancing source numbers could be
            # a repeated paragraph. It is not evidence that nothing was skipped.
            if overlap == len(current) and (first, last) != self.last_range:
                self.waiting += 1
                self.error = 'repeated_window'
                return
            self.rows.extend(current[overlap:])
            self.previous = current
            self.last_range = (first, last)
            self.revision += 1
        self.waiting = 0
        self.error = None
        if not at_end:
            return
        self.end = last
        folded, pending = self.fold(self.rows)
        # Footer offsets can settle back to zero at EOF. Accept the actual
        # final extent or the single extra line evidenced on the first screen.
        expected = last + self.initial_extra if len(folded) > last else last
        # Only remove unpainted padding beyond the corroborated source extent.
        # Interior and source-counted blank lines remain intact.
        while len(folded) > expected and padding(folded[-1]):
            folded.pop()
        if pending or len(folded) != expected:
            self.error = 'end_line_count_mismatch'
            return
        self.result = folded

    def diagnostics(self):
        return {'method': 'screen_overlap', 'display_rows': len(self.rows),
                'waiting_frames': self.waiting, 'error': self.error,
                'initial_extra': self.initial_extra, 'moves': list(self.moves),
                'complete': self.result is not None}
