"""Capture SGR colors alongside PyPtt's plain-text article reader (2.3.16)."""

from difflib import SequenceMatcher
from itertools import groupby
import re

from PyPtt import command, screens


SGR = re.compile(r'\x1b\[([0-9;]*)m')
LINE_RANGE = re.compile(r'^\s*瀏覽.*?第\s*(\d+)\s*~\s*(\d+)\s*行')
MAX_FRAME_BYTES = 256_000
MAX_FRAMES = 4096


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

    def __init__(self):
        self.rows = {}
        self.colors = {}
        self.seen = self.unnumbered = self.width_mismatches = self.conflicts = 0
        self.first_range = self.last_range = self.end_range = None
        self.first_conflict = None
        self.end_usable = False
        self.width_sample = []

    def observe(self, rows, status, *, at_end, styled):
        self.seen += 1
        match = LINE_RANGE.search(status)
        if not match:
            self.unnumbered += 1
            return

        first, last = map(int, match.groups())
        span = [first, last]
        if self.first_range is None:
            self.first_range = span
        self.last_range = span
        if at_end:
            self.end_range = span
        count = last - first + 1
        if first < 1 or not 0 < count <= len(rows) or any(rows[count:]):
            self.width_mismatches += 1
            if len(self.width_sample) < 12:
                self.width_sample.append({'screen': self.seen, 'range': span, 'rows': len(rows),
                                          'blank_rows': sum(not row.strip() for row in rows)})
            return
        if at_end:
            self.end_usable = True

        for offset, row in enumerate(rows[:count]):
            number = first + offset
            if self.rows.setdefault(number, row) != row:
                self.conflicts += 1
                if self.first_conflict is None:
                    self.first_conflict = number
                continue
            if styled is not None and number not in self.colors:
                self.colors[number] = styled[offset].color_runs

    def assemble(self):
        if not self.end_usable or self.conflicts or self.width_mismatches or self.unnumbered or not self.rows:
            return None
        end = self.end_range[1]
        if len(self.rows) != end or min(self.rows) != 1 or max(self.rows) != end:
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
            'screen_count': self.seen, 'unnumbered_screens': self.unnumbered,
            'width_mismatches': self.width_mismatches, 'overlap_conflicts': self.conflicts,
            'width_mismatches_sample': self.width_sample,
            'first_conflict_line': self.first_conflict, 'first_range': self.first_range,
            'last_range': self.last_range, 'end_range': self.end_range, 'end_usable': self.end_usable,
            'unique_lines': len(self.rows), 'missing_lines': sum(end - start + 1 for start, end in gaps),
            'missing_ranges_sample': gaps[:12],
        }


def read_colored_post(api, board, aid):
    """Observe received article screens only. Login and navigation stay outside capture."""
    core = api.connect_core
    send = core.send
    stream = core._stream_screen
    buffers = {}
    frames = []
    line_cache = {}
    numbered = NumberedArticle()
    capturing = False
    frame_limit_hit = False
    reached_end = False

    def capture_stream(encoding, chunk):
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
        nonlocal capturing, frame_limit_hit, reached_end, numbered
        article_targets = (screens.Target.PostEnd, screens.Target.InPost)
        article_screen = any(target.get_detect_target() in article_targets for target in targets)
        if article_screen and message == command.enter * 2:
            # PyPtt can retry from the beginning when metadata validation fails.
            # Keep completion and colors tied to the attempt it actually returns.
            frames.clear()
            line_cache.clear()
            numbered = NumberedArticle()
            reached_end = frame_limit_hit = False
        capturing = article_screen and numbered.seen < MAX_FRAMES
        frame_limit_hit |= article_screen and not capturing
        try:
            result = send(message, targets, *args, **kwargs)
            if article_screen and 0 <= result < len(targets):
                reached_end |= targets[result].get_detect_target() == screens.Target.PostEnd
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
                numbered.observe(rows[:-1], rows[-1], at_end=reached_end, styled=packed)
            return result
        finally:
            capturing = False

    core.send = capture_send
    core._stream_screen = capture_stream
    try:
        post = api.get_post(board, aid=aid)
    except Exception as exc:
        exc.capture_diagnostics = {'frames': len(frames), 'frame_limit_hit': frame_limit_hit,
                                   'reached_end': reached_end, 'numbering': numbered.diagnostics()}
        raise
    finally:
        core.send = send
        core._stream_screen = stream
    diagnostics = {'frames': len(frames), 'frame_limit_hit': frame_limit_hit,
                   'reached_end': reached_end, 'numbering': numbered.diagnostics()}
    post['ansi_diagnostics'] = diagnostics
    body = post.get('full_content')
    if body:
        sdk_rows = body.split('\n')
        reconstructed = numbered.assemble()
        if reconstructed is not None:
            body = post['full_content'] = reconstructed
            colors = {str(number - 1): list(runs) for number, runs in numbered.colors.items() if runs}
            covered = len(numbered.colors)
        else:
            colors, covered = match_colors(body, frames)
        selected_rows = body.split('\n')
        if colors:
            post['ansi_lines'] = colors
        post['ansi_coverage'] = {'matched': covered, 'total': len(selected_rows), 'colored': len(colors)}
        diagnostics.update(
            assembly='numbered' if reconstructed is not None else 'sdk',
            sdk_lines=len(sdk_rows), selected_lines=len(selected_rows),
            sdk_blank_lines=sum(not line.strip() for line in sdk_rows),
            selected_blank_lines=sum(not line.strip() for line in selected_rows),
            first_changed_line=next(
                (n for n, (old, new) in enumerate(zip(sdk_rows, selected_rows), 1) if old != new),
                min(len(sdk_rows), len(selected_rows)) + 1 if len(sdk_rows) != len(selected_rows) else None),
        )
    return post
