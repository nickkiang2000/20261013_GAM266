"""Capture SGR colors alongside PyPtt's plain-text article reader (2.3.16)."""

from collections import deque
from difflib import SequenceMatcher
from hashlib import sha256
from itertools import groupby
import re

from PyPtt import command, screens

from terminal_links import TerminalLinks


SGR = re.compile(r'\x1b\[([0-9;]*)m')
LINE_RANGE = re.compile(r'^\s*瀏覽.*?第\s*(\d+)\s*~\s*(\d+)\s*行')
SINGLE_SCREEN_END = re.compile(r'^\s*瀏覽\s*第\s*1\s*/\s*1\s*頁\s*\(\s*100%\s*\)')
MAX_FRAME_BYTES = 256_000
MAX_FRAMES = 4096
WRAP_MARKER_COLUMNS = (79, 80)


class StyledLine(str):
    def __new__(cls, text, screen, styles=()):
        line = super().__new__(cls, text)
        line.screen = screen
        line.styles = styles
        return line

    def __getitem__(self, index):
        result = super().__getitem__(index)
        if isinstance(index, slice):
            return StyledLine(result, self.screen, self.styles[index])
        return result

    def __add__(self, text):
        if isinstance(text, StyledLine):
            styles = text.styles
        else:
            # The SDK fills the gap after cursor positioning with ordinary
            # spaces before writing the next run. Those cells were never painted.
            unpainted = (text.startswith(' ') and text == ' ' *
                         (self.screen._cursor_x - screens._cell_len(self)))
            styles = (() if unpainted else self.screen.classes,) * len(text)
        return StyledLine(super().__add__(text), self.screen, self.styles + styles)

    def runs(self):
        runs = []
        start = 0
        for style, group in groupby(self.styles):
            end = start + sum(1 for _ in group)
            runs.append((str.__getitem__(self, slice(start, end)), style))
            start = end
        return runs


class CapturedLine(str):
    def __new__(cls, text, runs):
        line = super().__new__(cls, text)
        line.color_runs = runs
        return line


def is_screen_padding(line):
    return not line.strip() and not any(
        token.startswith('b') for _, style in line.color_runs for token in style)


def right_edge_marker(row, colors):
    last = row.rstrip(' ')
    if not last.endswith('\\'):
        return None
    column = screens._cell_len(last.replace('\x08', ''))
    if column < min(WRAP_MARKER_COLUMNS) - 1:
        return None
    glyph = len(last) - 1
    prefix_runs = []
    offset = 0
    for value, style in colors:
        if offset + len(value) > glyph:
            if offset < glyph:
                prefix_runs.append((value[:glyph - offset], style))
            return column, style, tuple(prefix_runs), offset == glyph and value.rstrip(' ') == '\\'
        prefix_runs.append((value, style))
        offset += len(value)
    return column, (), (), False


def fold_numbered_rows(rows, styled, first, count, known_markers, *, first_page=False, tail_hint=False, near_edge=False):
    """Use a distinct bright-white right-edge marker to join display rows."""
    if styled is None or count <= 0 or len(styled) != len(rows):
        return None
    logical = []
    text = ''
    runs = []
    markers = {}
    reused = 0
    last_visible = next((i for i in range(len(rows) - 1, -1, -1)
                         if not is_screen_padding(styled[i])), -1)
    for i, row in enumerate(rows):
        colors = styled[i].color_runs or (((row, ()),) if row else ())
        last = row.rstrip(' ')
        fragment = last[:-1] if last.endswith('\\') else last
        key = (first + len(logical), len(text), fragment)
        if key in known_markers:
            # A scrolling redraw can omit the marker or repaint it without
            # its SGR. Reuse only a fragment already seen at this source line
            # and offset, then check the assembled frame against its overlap.
            marker_colors = known_markers[key]
            reused += 1
        else:
            marker_colors = None
            marker = right_edge_marker(row, colors)
            if marker is not None:
                column, style, prefix_runs, separate = marker
                strong = column in WRAP_MARKER_COLUMNS and separate
                tentative = (tail_hint and i == last_visible and bool(prefix_runs)
                             and min(WRAP_MARKER_COLUMNS) - 1 <= column <= max(WRAP_MARKER_COLUMNS))
                near_marker = near_edge and separate and column == min(WRAP_MARKER_COLUMNS) - 1
                if set(style) == {'f7', 'hl'} and (strong or tentative or near_marker):
                    marker_colors = prefix_runs
        if marker_colors is not None:
            text += fragment
            runs.extend(marker_colors)
            markers[key] = marker_colors
        elif text and all(is_screen_padding(line) for line in styled[i:]):
            # The line continues below the screen. Its prefix is useful for
            # verifying the next frame but is not a complete source line.
            break
        else:
            if text:
                combined = tuple(runs) + colors
                logical.append(CapturedLine(text + row, combined if any(style for _, style in combined) else ()))
            else:
                logical.append(styled[i])
            text = ''
            runs = []
    pending = CapturedLine(text, tuple(runs)) if text else None
    if not markers:
        return None
    # Drop only trailing unpainted screen padding beyond the advertised range.
    complete = count - (pending is not None)
    retained = complete + 1 if first_page and pending is None and len(logical) == complete + 1 else complete
    if len(logical) < complete or any(not is_screen_padding(line) for line in logical[retained:]):
        return None
    if pending is not None and len(logical) > complete:
        # The physical screen may have padding immediately before an
        # unfinished line. Keep its marker on the normalized source line.
        pending_number = first + len(logical)
        markers = {
            (first + complete if number == pending_number else number, offset, fragment): colors
            for (number, offset, fragment), colors in markers.items()
        }
    # The first page can include one additional logical row, even after
    # folding. Keep the candidate until the next page proves the offset.
    return logical[:retained], pending, markers, reused


class ColorScreen(screens._VT100Engine):
    """Replay the SDK's cursor operations while attaching colors to written text."""

    def __init__(self, height):
        self.foreground = self.background = None
        self.bright = self.inverse = False
        super().__init__(height)

    def _reset_screen(self):
        super()._reset_screen()
        self._lines = [StyledLine('', self) for _ in self._lines]

    @property
    def classes(self):
        foreground, background = self.foreground, self.background
        if self.inverse:
            foreground = 0 if background is None else background
            background = 7 if self.foreground is None else self.foreground
        result = []
        if foreground is not None or self.bright:
            result.append(f'f{7 if foreground is None else foreground}')
        if background is not None:
            result.append(f'b{background}')
        if self.bright:
            result.append('hl')
        return tuple(result)

    def sgr(self, parameters):
        for value in parameters.split(';'):
            code = int(value or 0)
            if code == 0:
                self.foreground = self.background = None
                self.bright = self.inverse = False
            elif code == 1:
                self.bright = True
            elif code in (2, 22):
                self.bright = False
            elif code in (7, 27):
                self.inverse = code == 7
            elif 30 <= code <= 37:
                self.foreground = code - 30
            elif 40 <= code <= 47:
                self.background = code - 40
            elif code == 39:
                self.foreground = None
            elif code == 49:
                self.background = None
            elif 90 <= code <= 97:
                self.foreground = code - 90
                self.bright = True
            elif code in (38, 48):
                # Extended palettes use extra numeric fields. Do not interpret
                # those fields as independent SGR commands.
                break

    def replay(self, data, encoding):
        text = data.decode(encoding, errors='replace')
        start = 0
        for match in SGR.finditer(text):
            if self._run(screens._preprocess(text[start:match.start()])):
                return self._lines
            self.sgr(match[1])
            start = match.end()
        self._run(screens._preprocess(text[start:]))
        return self._lines


def match_colors(body, frames):
    lines = body.split('\n')
    output = {}
    covered = set()
    cursor = 0
    for frame in frames:
        window_start = max(0, cursor - len(frame))
        window_end = cursor + len(frame) * 2
        match = SequenceMatcher(None, lines[window_start:window_end], frame, autojunk=False)
        for block in match.get_matching_blocks():
            if block.size < 2 and len(frame) > 1:
                continue
            for index in range(block.size):
                position = window_start + block.a + index
                if position in covered:
                    continue
                covered.add(position)
                line = frame[block.b + index]
                if line.color_runs:
                    output[str(position)] = list(line.color_runs)
            if block.size:
                cursor = max(cursor, window_start + block.a + block.size)
    return output, len(covered)


class NumberedArticle:
    """Reassemble physical rows only when the terminal numbering proves their positions."""

    def __init__(self, *, page_mode=False, allow_wrapping=False, short_wrap_retry=False):
        self.page_mode = page_mode
        self.allow_wrapping = allow_wrapping
        self.short_wrap_retry = short_wrap_retry
        from scroll_capture import ScrollCapture
        self.scrolling = ScrollCapture() if short_wrap_retry else None
        self.pending_wrap = None
        self.wrap_markers = {}
        self.wrapped_screens = 0
        self.wrap_marker_seen = False
        self.wrap_joins = 0
        self.tail_wrap_hints = 0
        self.near_edge_wrap_frames = 0
        self.wrap_markers_reused = 0
        self.wrap_failure = None
        self.wrap_alignments = []
        self.wrap_window_checks = []
        self.extra_first_row = False
        self.first_padding_row = None
        self.provisional_row = None
        self.provisional_content = None
        self.bridge_distance = self.bridge_remaining = self.bridge_moves = 0
        self.bridge_next_start = self.bridge_min_end = None
        self.bridge_result = None
        self.page_line_offset = 0
        self.page_ranges = []
        self.single_screen_body = None
        self.rows = {}
        self.colors = {}
        self.color_conflict_rows = set()
        self.seen = self.unnumbered = self.width_mismatches = self.conflicts = 0
        self.first_range = self.last_range = self.end_range = None
        self.first_conflict = None
        self.end_usable = False
        self.width_sample = []
        self.last_window = None
        self.last_percent = None
        self.color_conflicts = 0
        self.end_evidence = None

    def observe(self, rows, status, *, at_end, styled, linked_screen=False):
        self.seen += 1
        self.last_window = None
        percent = re.search(r'\(\s*(\d{1,3})%\)', status)
        self.last_percent = int(percent[1]) if percent else None
        match = LINE_RANGE.search(status)
        bridging = self.bridge_remaining > 0
        if not match:
            self.unnumbered += 1
            if bridging:
                self.bridge_remaining = 0
                self.bridge_result = 'unnumbered_screen'
            return

        first, last = map(int, match.groups())
        span = [first, last]
        if self.first_range is None:
            self.first_range = span
        self.last_range = span
        if at_end:
            self.end_range = span
        count = last - first + 1
        if self.scrolling is not None:
            self.scrolling.observe(styled, first, last, at_end)
            from scroll_capture import wrap_prefix
            self.wrap_marker_seen |= any(wrap_prefix(row) is not None for row in self.scrolling.rows)
            selected = self.scrolling.result
            visible, _ = self.scrolling.fold(self.scrolling.rows)
            selected = selected if selected is not None else visible
            self.rows = {i: text for i, (text, _) in enumerate(selected, 1)}
            self.colors = {i: runs for i, (_, runs) in enumerate(selected, 1)}
            self.last_window = (1, len(selected), tuple(text for text, _ in selected)) if selected else None
            self.end_usable = self.scrolling.result is not None
            if self.end_usable:
                self.end_range = [1, len(selected)]
                self.end_evidence = 'screen_overlap_and_end_count'
            return
        seen_provisional = self.provisional_row
        prior_provisional = self.provisional_content
        checking_first_extra = (self.seen == 2 or bridging) and (
            self.extra_first_row or self.first_padding_row is not None)
        first_extra_candidate = (self.seen == 1 and first == 1 and not at_end
                                 and (self.page_mode or linked_screen or self.short_wrap_retry))
        if self.allow_wrapping:
            folded = fold_numbered_rows(rows, styled, first, count, self.wrap_markers,
                                        first_page=(first_extra_candidate or checking_first_extra
                                                    or (seen_provisional is not None and not at_end)))
            if (folded is None and styled is not None
                    and (not at_end or self.pending_wrap is not None or self.near_edge_wrap_frames)):
                candidate = fold_numbered_rows(rows, styled, first, count, self.wrap_markers, near_edge=True)
                if candidate is not None and (not at_end or candidate[1] is None):
                    overlap = self._check_wrapped_window(first + self.page_line_offset, candidate[0])
                    if ((self.seen == 1 and first == 1 and candidate[1] is None)
                            or (overlap['distinct_text_anchors'] and not overlap['conflicting_rows'])):
                        folded = candidate
                        self.near_edge_wrap_frames += 1
            if folded is None and styled is not None and self.wrap_markers and not at_end:
                candidate = fold_numbered_rows(rows, styled, first, count, self.wrap_markers, tail_hint=True)
                if candidate is not None and candidate[1] is not None:
                    overlap = self._check_wrapped_window(first + self.page_line_offset, candidate[0])
                    if overlap['distinct_text_anchors'] and not overlap['conflicting_rows']:
                        folded = candidate
                        self.tail_wrap_hints += 1
            if folded is None and styled is not None and self.wrap_markers:
                folded = self._align_wrapped_overlap(first, count, styled)
            if folded is not None:
                self.wrap_marker_seen = True
                if self.wrap_markers and not checking_first_extra and not self.page_line_offset:
                    nominal = self._check_wrapped_window(first, folded[0])
                    if nominal['conflicting_rows']:
                        verified = self._recover_wrapped_window(
                            first, count, rows, styled, folded, nominal, at_end=at_end)
                        if verified is None:
                            self.wrap_failure = 'overlap_not_verified'
                            self.conflicts += len(nominal['conflicting_rows'])
                            self.first_conflict = first + nominal['conflicting_rows'][0] - 1
                            return
                        first, folded = verified
                        last = first + len(folded[0]) - 1 + (folded[1] is not None)
                rows, pending, markers, reused = folded
                styled = rows
                if pending is not None and at_end:
                    self.wrap_failure = 'continuation_missing_at_end'
                    self.width_mismatches += 1
                    return
                if self.pending_wrap is not None:
                    pending_number, prefix = self.pending_wrap
                    prefix_colors = [style for value, style in prefix.color_runs for _ in value]
                    positions = {pending_number, pending_number + first - span[0]}
                    matches = 0
                    for number in positions:
                        offset = number - first
                        observed = (rows[offset] if 0 <= offset < len(rows)
                                    else pending if number == last else None)
                        if observed is None or not observed.startswith(prefix):
                            continue
                        observed_colors = ([style for value, style in observed.color_runs for _ in value]
                                           or [()] * len(observed))
                        matches += observed_colors[:len(prefix_colors)] == prefix_colors
                    if matches != 1:
                        self.wrap_failure = 'continuation_did_not_match'
                        self.width_mismatches += 1
                        return
                self.pending_wrap = (last, pending) if pending is not None else None
                self.wrap_markers.update(markers)
                self.wrapped_screens += 1
                self.wrap_joins += len(markers)
                self.wrap_markers_reused += reused
                count = last - first + 1 - (pending is not None)
            elif self.pending_wrap is not None:
                self.wrap_failure = 'continuation_not_observed'
                self.width_mismatches += 1
                self.record_width_sample(span, rows, styled)
                return
        if bridging:
            self.bridge_moves += 1
            if first != self.bridge_next_start or not 0 < count <= len(rows):
                self.bridge_remaining = 0
                self.bridge_result = 'unexpected_position'
                return
            self.bridge_remaining -= 1
            self.bridge_next_start -= 1
            if self.bridge_remaining:
                return
        extra = rows[max(count, 0):]
        padded = ((self.page_mode or first_extra_candidate) and not any(row.strip() for row in extra)
                  and (styled is None or not any(line.color_runs for line in styled[max(count, 0):])))
        # Some first screens contain one more display row than the reported span.
        # Keep it provisionally, then require a unique overlap on the next screen.
        if (first_extra_candidate and (self.page_mode or self.wrapped_screens or self.short_wrap_retry)
                and self.pending_wrap is None and count > 0 and len(extra) == 1):
            if padded:
                self.first_padding_row = (extra[0], styled[count].color_runs if styled is not None else None)
            else:
                self.extra_first_row = True
                count += 1
        elif checking_first_extra:
            candidates = []
            visible = rows[:count + 1] if len(rows) == count + 1 else rows[:count]
            if self._confirms_first_overlap(first, visible) or (
                    self.first_padding_row is not None and first == len(self.rows) + 1):
                candidates.append(0)
            extra_row = self.first_padding_row[0] if self.first_padding_row is not None else None
            if self._confirms_first_overlap(first + 1, visible, extra_row=extra_row):
                candidates.append(1)
            if len(candidates) != 1:
                if self.page_mode and not candidates and not bridging and extra_row is not None:
                    if self._plan_blank_bridge(first, last, visible, extra_row, at_end=at_end):
                        return
                if bridging:
                    self.bridge_result = 'overlap_unconfirmed'
                self.width_mismatches += 1
                self.width_sample.append({'screen': self.seen, 'range': span,
                                          'rows': len(rows), 'overlap_candidates': candidates})
                return
            self.page_line_offset = candidates[0]
            if (not self.page_line_offset and len(visible) == count + 1 and styled is not None
                    and not at_end and self.pending_wrap is None):
                overlap = self._check_wrapped_window(first, styled[:count + 1])
                # Keep a second extra row only while full overlap confirms
                # the start. A later frame must confirm the tentative row.
                if (overlap['distinct_text_anchors'] >= 2 and not overlap['conflicting_rows']
                        and first + count > max(self.rows, default=0)):
                    self.provisional_row = first + count
                    count += 1
            if bridging:
                self.bridge_result = 'aligned'
            if self.page_line_offset and self.first_padding_row is not None:
                row, runs = self.first_padding_row
                number = len(self.rows) + 1
                self.rows[number] = row
                if runs is not None:
                    self.colors[number] = runs
                self.extra_first_row = True
                if self.page_ranges:
                    self.page_ranges[0]['mapped'][1] = number
        if (seen_provisional is not None and not at_end and self.pending_wrap is None
                and len(rows) == count + 1 and styled is not None):
            start = first + self.page_line_offset
            overlap = self._check_wrapped_window(start, styled)
            if (start <= seen_provisional < start + count
                    and start + count == seen_provisional + 1
                    and overlap['distinct_text_anchors'] >= 2 and not overlap['conflicting_rows']
                    and prior_provisional == (rows[seen_provisional - start],
                                               styled[seen_provisional - start].color_runs)):
                self.provisional_row = start + count
                count += 1
        if first < 1 or not 0 < count <= len(rows) or (any(rows[count:]) and not padded):
            self.width_mismatches += 1
            # Match the SDK's first-and-final screen extraction. The status
            # confirms one screen from line 1; the row count can include padding.
            if (self.page_mode and self.seen == 1 and at_end and first == 1
                    and 0 < count <= len(rows) and SINGLE_SCREEN_END.match(status)
                    and any(row.strip() for row in rows)):
                self.single_screen_body = '\n'.join(rows)
            self.record_width_sample(span, rows, styled)
            if self.wrap_marker_seen:
                self.single_screen_body = None
            return
        first += self.page_line_offset
        last = first + count - 1
        if seen_provisional is not None and seen_provisional != self.provisional_row:
            # The additional row advances only after the former one matched.
            self.provisional_content = None
        elif seen_provisional is not None and first <= seen_provisional <= last and styled is not None:
            observed = styled[seen_provisional - first]
            if prior_provisional == (observed, observed.color_runs):
                self.provisional_row = self.provisional_content = None
        if self.page_mode and len(self.page_ranges) < 12:
            self.page_ranges.append({'reported': span, 'mapped': [first, last],
                                     'rows': len(rows), 'percent': self.last_percent})
        if at_end:
            self.end_range = [first, last]
            self.end_usable = True
        # A candidate may be observed while the preceding source line is
        # still incomplete. Do not enter it in the overlap map before proof.
        if self.provisional_row == last:
            self.provisional_content = (rows[count - 1], styled[count - 1].color_runs)
            count -= 1
        self.last_window = (first, first + count - 1, tuple(rows[:count]))

        for offset, row in enumerate(rows[:count]):
            number = first + offset
            if number == self.provisional_row:
                continue
            if self.rows.setdefault(number, row) != row:
                self.conflicts += 1
                if self.first_conflict is None:
                    self.first_conflict = number
                continue
            if styled is not None:
                runs = styled[offset].color_runs
                if self.colors.setdefault(number, runs) != runs:
                    self.color_conflicts += 1
                    self.color_conflict_rows.add(number)

    def _check_wrapped_window(self, first, rows):
        anchors = set()
        conflicts = []
        text_conflicts = []
        overlap = 0
        for index, row in enumerate(rows, first):
            if index not in self.rows:
                continue
            overlap += 1
            if self.rows[index] != row or self.colors.get(index) != row.color_runs:
                screen_row = index - first + 1
                conflicts.append(screen_row)
                if self.rows[index] != row:
                    text_conflicts.append(screen_row)
            elif row.strip():
                anchors.add(str(row))
        return {'first': first, 'overlap': overlap, 'distinct_text_anchors': len(anchors),
                'conflicting_rows': conflicts, 'text_conflicting_rows': text_conflicts}

    def _recover_wrapped_window(self, first, count, rows, styled, folded, nominal, *, at_end):
        candidates = [nominal]
        verified = []
        folded_rows, pending, markers, reused = folded
        # A blank at the top can occupy a row for an already verified line
        # while the wrap marker and the rest of the overlap stay in place.
        leading_padding = is_screen_padding(folded_rows[0])
        if nominal['conflicting_rows'] == [1] and leading_padding and first in self.rows:
            remaining = folded_rows[1:]
            evidence = self._check_wrapped_window(first + 1, remaining)
            evidence['method'] = 'leading_padding'
            candidates.append(evidence)
            if (not evidence['conflicting_rows'] and evidence['distinct_text_anchors'] >= 2
                    and any(marker in self.wrap_markers for marker in markers)):
                verified.append((first + 1, (remaining, pending, markers, reused)))
        for start in (first - 1, first + 1):
            if start < 1:
                continue
            same_end_count = first + count - start
            attempts = [('keep_reported_end', self._align_wrapped_overlap(start, same_end_count, styled))]
            if not at_end:
                attempts.append(('shift_range', fold_numbered_rows(rows, styled, start, count, self.wrap_markers)))
            for method, folded in attempts:
                if folded is None:
                    continue
                evidence = self._check_wrapped_window(start, folded[0])
                evidence['method'] = method
                candidates.append(evidence)
                if (not evidence['conflicting_rows'] and evidence['distinct_text_anchors'] >= 2
                        and any(marker in self.wrap_markers for marker in folded[2])):
                    if not any(
                            start == previous_start and folded[1:] == previous[1:]
                            and [(row, row.color_runs) for row in folded[0]]
                            == [(row, row.color_runs) for row in previous[0]]
                            for previous_start, previous in verified):
                        verified.append((start, folded))
        selection = verified[0] if len(verified) == 1 else None
        if len(self.wrap_window_checks) >= 12:
            self.wrap_window_checks.pop(0)
        self.wrap_window_checks.append({
            'screen': self.seen, 'reported_range': [first, first + count - 1],
            'leading_row_blank': not folded_rows[0].strip(), 'leading_row_padding': leading_padding,
            'candidates': candidates, 'selected_start': selection[0] if selection else None,
        })
        return selection

    def _align_wrapped_overlap(self, first, count, styled):
        """Confirm a one-row shift before an already identified wrap."""
        for number, offset, fragment in self.wrap_markers:
            expected = number - first
            if offset or not 0 < expected < count or expected + 2 >= len(styled):
                continue
            marker_row = styled[expected + 1].rstrip(' ')
            if marker_row not in (fragment, fragment + '\\'):
                continue
            lines = range(first, number)
            if any(line not in self.rows or line not in self.colors for line in lines):
                continue
            previous = [(self.rows[line], self.colors[line]) for line in lines]
            if not any(text.strip() for text, _ in previous):
                continue
            before_marker = styled[:expected + 1]
            observed = [(line, line.color_runs) for line in before_marker]
            candidates = [i for i in range(len(observed))
                          if observed[:i] + observed[i + 1:] == previous]
            removable = [i for i in candidates if is_screen_padding(before_marker[i]) or (
                i == 0 and first - 1 in self.colors
                and observed[i] == (self.rows[first - 1], self.colors[first - 1]))]
            # Removing either of two identical padding rows yields the same
            # verified overlap. Require the content and colors to be identical.
            folded = None
            if removable:
                index = removable[0]
                corrected = styled[:index] + styled[index + 1:]
                folded = fold_numbered_rows(corrected, corrected, first, count, self.wrap_markers)
            if len(self.wrap_alignments) < 12:
                self.wrap_alignments.append({
                    'screen': self.seen, 'source_line': number,
                    'mapped_start': first,
                    'expected_marker_row': expected + 1, 'observed_marker_row': expected + 2,
                    'matching_drop_rows': [i + 1 for i in candidates],
                    'padding_or_preceding_rows': [i + 1 for i in removable],
                    'first_text_mismatch_row': next(
                        (i + 1 for i, ((text, _), (old, _)) in enumerate(zip(observed, previous))
                         if text != old), None),
                    'observed_rows_before_marker': [
                        {'row': i + 1, 'blank': not line.strip(), 'padding': is_screen_padding(line)}
                        for i, line in enumerate(before_marker)],
                    'aligned': folded is not None,
                })
            if folded is not None:
                return folded
        return None

    def record_width_sample(self, span, rows, styled):
        if len(self.width_sample) >= 12:
            return
        sample = {'screen': self.seen, 'range': span, 'rows': len(rows),
                  'blank_rows': sum(not row.strip() for row in rows)}
        if self.allow_wrapping:
            sample['styles_available'] = styled is not None
            sample['backslash_rows_at_right_edge'] = [
                {'row': offset + 1, 'last_style': styled[offset].color_runs[-1][1]
                 if styled is not None and styled[offset].color_runs else ()}
                for offset, row in enumerate(rows)
                if (last_text := row.rstrip(' ')).endswith('\\')
                and screens._cell_len(last_text.replace('\x08', '')) in WRAP_MARKER_COLUMNS]
            sample['near_edge_backslashes'] = []
            for offset, line in enumerate(styled or ()):
                marker = right_edge_marker(line, line.color_runs)
                if marker is not None:
                    column, style, _, separate = marker
                    sample['near_edge_backslashes'].append({'row': offset + 1, 'column': column,
                                                            'style': style, 'separate_run': separate})
            if self.pending_wrap:
                pending_number, prefix = self.pending_wrap
                sample['pending_line_expected_screen_row'] = pending_number - span[0] + 1
                sample['pending_prefix_rows'] = [offset + 1 for offset, row in enumerate(rows)
                                                 if row.rstrip(' ') in (prefix, prefix + '\\')]
                sample['pending_marker_source_lines'] = sorted({
                    number for number, offset, fragment in self.wrap_markers
                    if offset == 0 and prefix.startswith(fragment)})
                sample['previous_overlap_rows'] = sum(
                    line in self.rows and line in self.colors for line in range(span[0], pending_number))
            self.wrap_marker_seen |= any(set(row['last_style']) == {'f7', 'hl'}
                                         for row in sample['backslash_rows_at_right_edge'])
        self.width_sample.append(sample)

    def _matching_first_overlap(self, first, rows, *, extra_row=None):
        end = max(self.rows, default=0)
        candidate_end = end + (extra_row is not None)
        if not 1 <= first <= candidate_end < first + len(rows) - 1:
            return []
        expected = [self.rows[number] for number in range(first, end + 1)]
        if extra_row is not None:
            expected.append(extra_row)
        return expected if rows[:len(expected)] == expected else []

    def _confirms_first_overlap(self, first, rows, *, extra_row=None):
        return any(row.strip() for row in self._matching_first_overlap(first, rows, extra_row=extra_row))

    def _plan_blank_bridge(self, first, last, rows, extra_row, *, at_end):
        # Probe only the specific one-row shift whose overlap matches but has
        # no text to anchor it. Preserve blanks and verify the wider overlap.
        shifted_start = first + 1
        overlap = self._matching_first_overlap(shifted_start, rows, extra_row=extra_row)
        if not overlap or any(row.strip() for row in overlap):
            return False
        anchor = next((number for number in range(shifted_start - 1, 0, -1)
                       if self.rows[number].strip()), None)
        if anchor is None:
            return False
        distance = shifted_start - anchor
        # Even rereading through the current screen would take at least this
        # many row steps. Leave room for one forward page after moving up.
        if first - distance < 1 or distance + 1 >= last - len(self.rows):
            return False
        self.bridge_distance = self.bridge_remaining = distance
        self.bridge_next_start = first - 1
        self.bridge_min_end = last + 1 if at_end else None
        self.bridge_result = 'checking'
        return True

    def extends_pending_wrap(self, previous):
        if not previous or not self.pending_wrap:
            return False
        number, prefix = self.pending_wrap
        return number == previous[0] and len(prefix) > len(previous[1]) and prefix.startswith(previous[1])

    def covers_prefix(self, end):
        return (not (self.conflicts or self.width_mismatches or self.unnumbered)
                and end >= 1 and len(self.rows) >= end
                and all(number in self.rows for number in range(1, end + 1)))

    def covers(self, end):
        return (not (self.conflicts or self.width_mismatches or self.unnumbered)
                and len(self.rows) == end and min(self.rows, default=0) == 1
                and max(self.rows, default=0) == end)

    def confirm_end(self, window):
        if (self.pending_wrap is not None or window is None or window != self.last_window
                or self.color_conflicts or not self.covers(window[1])):
            return None
        self.end_range = list(window[:2])
        self.end_usable = True
        self.end_evidence = 'end_key_round_trip'
        return self.assemble()

    def assemble(self):
        if self.scrolling is not None:
            selected = self.scrolling.result
            return '\n'.join(text for text, _ in selected) if selected is not None else None
        if (self.pending_wrap is not None or self.provisional_row is not None
                or not self.end_usable or not self.covers(self.end_range[1])):
            return None
        end = self.end_range[1]
        if self.bridge_min_end is not None and end < self.bridge_min_end:
            return None
        return '\n'.join(self.rows[number] for number in range(1, end + 1))

    def diagnostics(self):
        last = self.end_range[1] if self.end_range else max(self.rows, default=0)
        gaps = []
        previous = 0
        for number in sorted(self.rows):
            if number > last:
                break
            if number > previous + 1:
                gaps.append([previous + 1, number - 1])
            previous = number
        if previous < last:
            gaps.append([previous + 1, last])
        return {
            'screen_overlap': self.scrolling.diagnostics() if self.scrolling is not None else None,
            'screen_count': self.seen, 'unnumbered_screens': self.unnumbered,
            'width_mismatches': self.width_mismatches, 'overlap_conflicts': self.conflicts,
            'overlap_color_conflicts': self.color_conflicts, 'end_evidence': self.end_evidence,
            'wrapped_screens': self.wrapped_screens, 'wrap_joins': self.wrap_joins,
            'wrap_marker_seen': self.wrap_marker_seen,
            'tail_wrap_hints': self.tail_wrap_hints,
            'near_edge_wrap_frames': self.near_edge_wrap_frames,
            'provisional_row': self.provisional_row,
            'wrap_markers_reused': self.wrap_markers_reused,
            'wrap_alignments': self.wrap_alignments,
            'wrap_window_checks': self.wrap_window_checks,
            'pending_wrap_line': self.pending_wrap[0] if self.pending_wrap else None,
            'wrap_failure': self.wrap_failure,
            'last_percent': self.last_percent,
            'width_mismatches_sample': self.width_sample,
            'extra_first_row': self.extra_first_row, 'page_line_offset': self.page_line_offset,
            'page_ranges_sample': self.page_ranges,
            'page_bridge': {'planned_up': self.bridge_distance, 'observed_up': self.bridge_moves,
                            'result': self.bridge_result} if self.bridge_distance else None,
            'first_conflict_line': self.first_conflict, 'first_range': self.first_range,
            'last_range': self.last_range, 'end_range': self.end_range, 'end_usable': self.end_usable,
            'unique_lines': len(self.rows), 'missing_lines': sum(end - start + 1 for start, end in gaps),
            'missing_ranges_sample': gaps[:12],
        }


def read_colored_post(api, board, aid):
    """Observe received article screens only. Login and navigation stay outside capture."""
    from board_session import BoardSession, END_KEY, FALLBACK_HEIGHT, PAGE_DOWN_KEY

    core = api.connect_core
    send = core.send
    stream = core._stream_screen
    buffers = {}
    frames = []
    terminal_steps = deque(maxlen=6)
    observed_rows = deque(maxlen=3)
    capture_samples = deque(maxlen=3)
    link_decoders = {}
    link_stream = None
    line_cache = {}
    mode = getattr(api, 'capture_mode', 'line') if isinstance(api, BoardSession) else 'line'
    def new_numbering():
        return NumberedArticle(page_mode=mode == 'page', allow_wrapping=True, short_wrap_retry=(
            isinstance(api, BoardSession) and mode == 'line'
            and core.config.screen_height == FALLBACK_HEIGHT))

    numbered = new_numbering()
    if isinstance(api, BoardSession):
        api.bridge_events.clear()
    capturing = False
    frame_limit_hit = False
    reached_end = False
    page_turns = 0
    fallback_attempts = []
    received_bytes = 0
    key_names = {None: 'receive_only', command.enter * 2: 'open', command.down: 'down',
                 command.up: 'up', command.right: 'right', command.left: 'left',
                 PAGE_DOWN_KEY: 'page_down', END_KEY: 'end', 'n': 'decline_animation'}

    def record_step(message, outcome, silent=None):
        queue = core.get_screen_queue()
        screen = queue[-1] if queue else ''
        footer = screen.rsplit('\n', 1)[-1]
        extent = LINE_RANGE.search(footer)
        percent = re.search(r'\(\s*(\d{1,3})%\)', footer) if extent else None
        terminal_steps.append({
            'key': key_names.get(message, 'other'), 'result': outcome, 'silent_timeout': silent,
            'received_bytes': received_bytes, 'height': core.config.screen_height,
            'screen_rows': screen.count('\n') + bool(screen),
            'article_range': list(map(int, extent.groups())) if extent else None,
            'percent': int(percent[1]) if percent else None,
            'screen_digest': sha256(screen.encode()).hexdigest()[:16] if screen else None,
        })

    def diagnostics():
        return {'mode': mode, 'page_key': 'Page Down' if mode == 'page' else None,
                'page_turns': page_turns, 'frames': len(frames), 'frame_limit_hit': frame_limit_hit,
                'reached_end': reached_end or numbered.end_evidence is not None,
                'numbering': numbered.diagnostics(), 'end_fallback': fallback_attempts,
                'osc8_commands': max((decoder.commands for decoder in link_decoders.values()), default=0),
                'hyperlink_targets': sorted({target for decoder in link_decoders.values() for target in decoder.targets}),
                'bridge_navigation': list(api.bridge_events) if isinstance(api, BoardSession) else [],
                'recent_row_layouts': [
                    {'range': span, 'rows': [
                        {'row': i, 'width': screens._cell_len(row), 'blank': not row.strip(),
                         'backslash_column': screens._cell_len(row.rstrip(' ')) if row.rstrip(' ').endswith('\\') else None,
                         'digest': sha256(row.encode()).hexdigest()[:12] if row.strip() else None}
                        for i, row in enumerate(rows, 1)]}
                    for span, rows in observed_rows
                ] if not reached_end or numbered.assemble() is None else [],
                'terminal_steps': list(terminal_steps) if not reached_end or numbered.assemble() is None else []}

    def capture_stream(encoding, chunk):
        nonlocal link_stream, received_bytes
        if capturing and encoding == core.current_encoding:
            received_bytes += len(chunk)
        if link_stream is not core._stream_parsers:
            link_stream = core._stream_parsers
            for decoder in link_decoders.values():
                decoder.pending = b''
                decoder.target = ''
                decoder.label.clear()
        decoder = link_decoders.get(encoding)
        if decoder is None:
            decoder = link_decoders[encoding] = TerminalLinks()
        chunk = decoder.feed(chunk)
        result = stream(encoding, chunk)
        if capturing:
            parser = core._stream_parsers.get(encoding)
            previous, data = buffers.get(encoding, (None, None))
            if previous is not parser:
                data = bytearray()
            if data is not None:
                if len(data) + len(chunk) <= MAX_FRAME_BYTES:
                    data.extend(chunk)
                else:
                    data = None
            buffers[encoding] = parser, data
        return result

    def capture_send(message, targets, *args, **kwargs):
        nonlocal capturing, frame_limit_hit, reached_end, numbered, page_turns, received_bytes
        article_targets = (screens.Target.PostEnd, screens.Target.InPost)
        article_screen = any(target.get_detect_target() in article_targets for target in targets)
        if article_screen and message == command.enter * 2:
            # PyPtt can retry from the beginning when metadata validation fails.
            # Keep completion and colors tied to the attempt it actually returns.
            frames.clear()
            observed_rows.clear()
            capture_samples.clear()
            line_cache.clear()
            for decoder in link_decoders.values():
                decoder.links.clear()
                decoder.targets.clear()
            numbered = new_numbering()
            reached_end = frame_limit_hit = False
            page_turns = 0
        if mode == 'page' and article_screen and message == PAGE_DOWN_KEY:
            page_turns += 1
        capturing = article_screen and numbered.seen < MAX_FRAMES
        frame_limit_hit |= article_screen and not capturing
        command_counts = {encoding: decoder.commands for encoding, decoder in link_decoders.items()}
        received_bytes = 0
        try:
            try:
                result = send(message, targets, *args, **kwargs)
            except Exception as exc:
                if article_screen:
                    record_step(message, type(exc).__name__)
                raise
            if article_screen:
                record_step(message, result, bool(getattr(core, 'last_timeout_was_silent', False)) if result < 0 else None)
            if article_screen and 0 <= result < len(targets):
                target = targets[result].get_detect_target()
                reached_end |= target == screens.Target.PostEnd
                if target == screens.Target.Animation:
                    terminal_steps[-1]['prompt'] = 'animation'
            if capturing and 0 <= result < len(targets) and targets[result].get_detect_target() in article_targets:
                _, data = buffers.get(core.current_encoding, (None, None))
                plain = core.get_screen_queue()[-1]
                rows = plain.split('\n')
                packed = None
                if data:
                    try:
                        frame = ColorScreen(core.config.screen_height).replay(data, core.current_encoding)
                        # Keep SDK navigation and plain text independent. A frame
                        # that disagrees with the SDK cannot supply reliable colors.
                        if '\n'.join(frame) == plain:
                            packed = []
                            for line in frame[:-1]:
                                runs = tuple(line.runs()) if any(line.styles) else ()
                                key = (str(line), runs)
                                if key not in line_cache:
                                    line_cache[key] = CapturedLine(*key)
                                packed.append(line_cache[key])
                            frames.append(packed)
                    except (ValueError, IndexError, LookupError):
                        pass
                at_end = targets[result].get_detect_target() == screens.Target.PostEnd
                decoder = link_decoders.get(core.current_encoding)
                linked_screen = bool(decoder and decoder.commands > command_counts.get(core.current_encoding, 0))
                if (isinstance(api, BoardSession) and mode == 'line'
                        and core.config.screen_height == FALLBACK_HEIGHT and numbered.scrolling is None):
                    from scroll_capture import ScrollCapture
                    numbered.scrolling = ScrollCapture()
                numbered.observe(rows[:-1], rows[-1], at_end=at_end, styled=packed, linked_screen=linked_screen)
                observed_rows.append((numbered.last_range, packed or rows[:-1]))
                capture_samples.append({
                    'screen': plain, 'encoding': core.current_encoding, 'height': core.config.screen_height,
                    'key': key_names.get(message, 'other'), 'at_end': at_end, 'linked_screen': linked_screen,
                    'colors': [list(line.color_runs) for line in packed] if packed is not None else None,
                    'raw': bytes(data) if data else b'',
                })
            return result
        finally:
            capturing = False

    def save_diagnostic(capture, reason):
        if capture_samples:
            from local_state import save_reading_capture
            try:
                path = save_reading_capture(board, aid, mode, list(capture_samples), capture, reason)
            except OSError as exc:
                capture['reading_capture_error'] = type(exc).__name__
            else:
                capture['reading_capture'] = str(path)

    core.send = capture_send
    core._stream_screen = capture_stream
    try:
        if isinstance(api, BoardSession):
            post = api.get_post(board, aid=aid, get_numbering=lambda: numbered, fallback_attempts=fallback_attempts)
        else:
            post = api.get_post(board, aid=aid)
    except Exception as exc:
        exc.capture_diagnostics = diagnostics()
        save_diagnostic(exc.capture_diagnostics, type(exc).__name__)
        if getattr(exc, 'open_screen_unrecognized', False):
            exc.capture_diagnostics['open_screen_unrecognized'] = True
        raise
    finally:
        core.send = send
        core._stream_screen = stream
    capture = diagnostics()
    post['ansi_diagnostics'] = capture
    body = post.get('full_content')
    if body:
        sdk_rows = body.split('\n')
        reconstructed = numbered.assemble()
        if reconstructed is not None:
            body = post['full_content'] = reconstructed
            reliable = numbered.colors.keys()
            if mode == 'page':
                reliable = reliable - numbered.color_conflict_rows
            colors = {str(number - 1): list(numbered.colors[number]) for number in sorted(reliable)
                      if numbered.colors[number]}
            covered = len(reliable)
        else:
            colors, covered = match_colors(body, frames)
        selected_rows = body.split('\n')
        decoder = link_decoders.get(core.current_encoding)
        targets = {label: next(iter(addresses)) for label, addresses in (decoder.links.items() if decoder else ())
                   if len(addresses) == 1 and label not in addresses and label in body}
        if targets:
            post['link_targets'] = targets
        if colors:
            post['ansi_lines'] = colors
        post['ansi_coverage'] = {'matched': covered, 'total': len(selected_rows), 'colored': len(colors)}
        assembly = ('screen_overlap' if numbered.scrolling is not None else 'numbered') if reconstructed is not None else 'sdk'
        if numbered.single_screen_body is not None:
            assembly = 'single_screen'
        capture.update(
            assembly=assembly,
            sdk_lines=len(sdk_rows), selected_lines=len(selected_rows),
            sdk_blank_lines=sum(not line.strip() for line in sdk_rows),
            selected_blank_lines=sum(not line.strip() for line in selected_rows),
            first_changed_line=next(
                (n for n, (old, new) in enumerate(zip(sdk_rows, selected_rows), 1) if old != new),
                min(len(sdk_rows), len(selected_rows)) + 1 if len(sdk_rows) != len(selected_rows) else None),
        )
    if body and numbered.assemble() is None and numbered.single_screen_body is None:
        save_diagnostic(capture, 'Text or color alignment needs review')
    return post
