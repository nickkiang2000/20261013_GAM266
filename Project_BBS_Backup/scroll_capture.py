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
        self.known_markers = {}
        self.offsets = deque(maxlen=12)
        self.wrapped_lines = []
        self.first_failure = None
        self.last_accepted = None
        self.end_without_extent = False

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

    def row_key(self, row):
        text, runs = row
        marker = wrap_prefix(row)
        base = marker[0] if marker else text.rstrip(' ').removesuffix('\\')
        if marker:
            self.known_markers[base] = marker[1]
        if base in self.known_markers and text.rstrip(' ') in (base, base + '\\'):
            text, runs = base, self.known_markers[base]
        # Adjacent SGR runs with the same color are equivalent.
        merged = []
        for value, style in runs:
            if merged and merged[-1][1] == style:
                merged[-1] = (merged[-1][0] + value, style)
            else:
                merged.append((value, style))
        return text, tuple(merged)

    def seam(self, current):
        previous = [self.row_key(row) for row in self.previous]
        observed = [self.row_key(row) for row in current]
        candidates = overlap_candidates(previous, observed)
        if candidates:
            return [(size, 0) for size in candidates]
        # A bottom cell outside the footer's advertised extent can be blank
        # until the next redraw. Require unique overlap and two text anchors.
        folded, pending = self.fold(self.previous)
        allowance = (max(0, len(folded) + int(pending) - (self.last_range[1]-self.last_range[0]+1))
                     if self.last_range else 0)
        choices = []
        for trim in range(1, min(allowance, len(previous)-2) + 1):
            if not all(padding(row) for row in self.previous[-trim:]):
                break
            choices.extend((size, trim) for size in overlap_candidates(previous[:-trim], observed))
        return choices

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
            self.last_accepted = current
            folded, pending = self.fold(current)
            # A first screen can expose one extra source line. A blank final
            # cell alone cannot establish that offset.
            self.initial_extra = int(len(folded) + int(pending) == last + 1 and not padding(current[-1]))
            self.revision += 1
        elif current == self.previous and ((first, last) == self.last_range or first is None):
            pass
        else:
            choices = self.seam(current)
            candidates = [size for size, _ in choices]
            self.moves.append({'reported': [first, last], 'overlaps': candidates,
                               'padding_trims': [trim for _, trim in choices]})
            if len(choices) != 1 or (first is not None and self.last_range and first < self.last_range[0]):
                if self.first_failure is None:
                    self.first_failure = {'accepted': self.previous, 'observed': current,
                                          'reported': [first, last]}
                self.waiting += 1
                self.error = 'overlap_ambiguous' if candidates else 'overlap_missing'
                return
            overlap, trim = choices[0]
            # A fully identical window with advancing source numbers could be
            # a repeated paragraph. It is not evidence that nothing was skipped.
            if overlap == len(current) and first is not None and (first, last) != self.last_range:
                self.waiting += 1
                self.error = 'repeated_window'
                return
            if trim:
                del self.rows[-trim:]
            self.rows.extend(current[overlap:])
            self.previous = current
            self.last_accepted = current
            if first is not None:
                self.last_range = (first, last)
            self.revision += 1
        self.waiting = 0
        self.error = None
        folded, pending = self.fold(self.rows)
        difference = len(folded) - last if last is not None else None
        if not at_end:
            if last is not None and not pending and current and not padding(current[-1]):
                self.offsets.append(difference)
            return
        if last is None:
            # Only the recognized personalized footer reaches this path.
            # PostEnd plus an unbroken, colored screen chain provides EOF
            # evidence even though the station deliberately hides numbering.
            if pending or self.previous is None or self.revision < 2:
                self.error = 'end_extent_unavailable'
                return
            self.end_without_extent = True
            last = len(folded)
            difference = 0
        self.end = last
        # Footer offsets can settle back to zero at EOF. Accept the actual
        # final extent or the single extra line evidenced on the first screen.
        corroborated = (len(self.offsets) >= 2 and self.offsets[-1] == self.offsets[-2] == difference
                        and self.revision >= 3 and abs(difference) <= len(current))
        expected = len(folded) if corroborated else last + self.initial_extra if len(folded) > last else last
        # Only remove unpainted padding beyond the corroborated source extent.
        # Interior and source-counted blank lines remain intact.
        while len(folded) > expected and padding(folded[-1]):
            folded.pop()
        if pending or len(folded) != expected:
            self.error = 'end_line_count_mismatch'
            return
        self.result = folded
        self.wrapped_lines = []
        logical = 0
        for row in self.rows:
            if wrap_prefix(row) is not None:
                if logical not in self.wrapped_lines:
                    self.wrapped_lines.append(logical)
            else:
                logical += 1

    def diagnostics(self):
        return {'method': 'screen_overlap', 'display_rows': len(self.rows),
                'waiting_frames': self.waiting, 'error': self.error,
                'initial_extra': self.initial_extra, 'moves': list(self.moves),
                'end_without_extent': self.end_without_extent,
                'complete': self.result is not None, 'observed_offsets': list(self.offsets),
                'first_failed_seam': self.first_failure,
                'accepted_display_rows': self.rows if self.error and self.end is not None else None}
