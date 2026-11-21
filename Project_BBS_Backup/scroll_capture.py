"""Join scrolling screens by visible overlap, independently of footer line offsets."""
from collections import deque
from PyPtt import screens


def padding(row):
    text, runs = row
    return not text.strip() and not any(token.startswith('b') for _, style in runs for token in style)


def visible_row(row):
    """Remove only trailing backspace residue after terminal rendering.

    Interior controls and all spaces remain untouched. Colored text must
    still describe the same visible string; raw traffic stays in diagnostics.
    """
    text, runs = row
    visible = text.rstrip('\x08')
    if visible == text:
        return text, runs
    painted = ''.join(value for value, _ in runs)
    if not runs or painted == visible:
        return visible, runs
    if painted != text:
        return text, runs
    trimmed = []
    offset = 0
    for value, style in runs:
        if offset >= len(visible):
            break
        trimmed.append((value[:len(visible)-offset], style))
        offset += len(value)
    return visible, tuple(trimmed)


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
        self.last_failure = None
        self.trailing_controls_removed = 0
        self.end_padding_rows = 0
        self.expected_end_lines = None

    @staticmethod
    def fold(rows):
        result = []
        text, runs = '', ()
        continuing = False
        for row in rows:
            row = visible_row(row)
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

    def resolve_ambiguity(self, choices, first):
        if (len(choices) <= 1 or first is None or self.last_range is None
                or len(self.offsets) < 2 or self.offsets[-1] != self.offsets[-2]):
            return choices, False
        advance = first - self.last_range[0]
        if advance <= 0:
            return choices, False
        supported = []
        for overlap, trim in choices:
            removed = len(self.previous) - trim - overlap
            lines, pending = self.fold(self.previous[:removed])
            if not pending and len(lines) == advance:
                supported.append((overlap, trim))
        return (supported, True) if len(supported) == 1 else (choices, False)

    def record_failure(self, current, first, last, reason):
        snapshot = {'accepted': self.previous, 'observed': current,
                    'accepted_range': self.last_range, 'reported': [first, last], 'reason': reason}
        if self.first_failure is None:
            self.first_failure = snapshot
        self.last_failure = snapshot

    def observe(self, styled, first, last, at_end):
        self.result = None
        if styled is None:
            self.waiting += 1
            self.error = 'colors_unavailable'
            return
        raw = [(str(row), tuple((text, tuple(style)) for text, style in row.color_runs)) for row in styled]
        current = [visible_row(row) for row in raw]
        self.trailing_controls_removed += sum(len(old[0])-len(new[0]) for old, new in zip(raw, current))
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
            choices, resolved = self.resolve_ambiguity(choices, first)
            self.moves.append({'reported': [first, last], 'overlaps': candidates,
                               'padding_trims': [trim for _, trim in choices],
                               'resolved_by_source_advance': resolved,
                               'selected_overlap': choices[0][0] if len(choices) == 1 else None})
            if len(choices) != 1 or (first is not None and self.last_range and first < self.last_range[0]):
                self.record_failure(current, first, last, 'overlap_ambiguous' if candidates else 'overlap_missing')
                self.waiting += 1
                self.error = 'overlap_ambiguous' if candidates else 'overlap_missing'
                return
            overlap, trim = choices[0]
            # A fully identical window with advancing source numbers could be
            # a repeated paragraph. It is not evidence that nothing was skipped.
            if overlap == len(current) and first is not None and (first, last) != self.last_range:
                self.waiting += 1
                self.error = 'repeated_window'
                self.record_failure(current, first, last, self.error)
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
        # Source numbering is corroborating evidence. Work out the expected
        # extent from prior connected screens BEFORE considering trailing blanks.
        stable_offset = (self.offsets[-1] if len(self.offsets) >= 2
                         and self.offsets[-1] == self.offsets[-2]
                         and self.revision >= 3 and abs(self.offsets[-1]) <= len(current) else None)
        if difference == 0:
            expected = last
        elif stable_offset is not None:
            expected = last + stable_offset
        else:
            expected = last + self.initial_extra if len(folded) > last else last
        self.expected_end_lines = expected
        extra = folded[max(expected, 0):]
        if (pending or expected < 1 or len(folded) < expected
                or any(not padding(row) for row in extra)):
            self.error = 'end_line_count_mismatch'
            return
        # Retain harmless tail blanks in the stored body, rather than refusing
        # or deleting them just to force equality with the footer count.
        self.end_padding_rows = len(extra)
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
                'first_failed_seam': self.first_failure, 'last_failed_seam': self.last_failure,
                'trailing_controls_removed': self.trailing_controls_removed,
                'end_padding_rows_retained': self.end_padding_rows, 'expected_end_lines': self.expected_end_lines,
                'accepted_display_rows': self.rows if self.error and self.end is not None else None}
