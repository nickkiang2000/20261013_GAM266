"""Capture SGR colors alongside PyPtt's plain-text article reader (2.3.16)."""

from difflib import SequenceMatcher
from itertools import groupby
import re

from PyPtt import command, screens


SGR = re.compile(r'\x1b\[([0-9;]*)m')
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


def read_colored_post(api, board, aid):
    """Observe received article screens only. Login and navigation stay outside capture."""
    core = api.connect_core
    send = core.send
    stream = core._stream_screen
    buffers = {}
    frames = []
    line_cache = {}
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
        nonlocal capturing, frame_limit_hit, reached_end
        article_targets = (screens.Target.PostEnd, screens.Target.InPost)
        article_screen = any(target.get_detect_target() in article_targets for target in targets)
        if article_screen and message == command.enter * 2:
            # PyPtt can retry from the beginning when metadata validation fails.
            # Keep completion and colors tied to the attempt it actually returns.
            frames.clear()
            line_cache.clear()
            reached_end = frame_limit_hit = False
        capturing = article_screen and len(frames) < MAX_FRAMES
        frame_limit_hit |= article_screen and not capturing
        try:
            result = send(message, targets, *args, **kwargs)
            if article_screen and 0 <= result < len(targets):
                reached_end |= targets[result].get_detect_target() == screens.Target.PostEnd
            if capturing and 0 <= result < len(targets) and targets[result].get_detect_target() in article_targets:
                _, data = buffers.get(core.current_encoding, (None, None))
                if data:
                    plain = core.get_screen_queue()[-1]
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
            return result
        finally:
            capturing = False

    core.send = capture_send
    core._stream_screen = capture_stream
    try:
        post = api.get_post(board, aid=aid)
    finally:
        core.send = send
        core._stream_screen = stream
    body = post.get('full_content')
    if body:
        colors, covered = match_colors(body, frames)
        if colors:
            post['ansi_lines'] = colors
        post['ansi_coverage'] = {'matched': covered, 'total': len(body.split('\n')), 'colored': len(colors)}
        post['ansi_diagnostics'] = {'frames': len(frames), 'frame_limit_hit': frame_limit_hit,
                                    'reached_end': reached_end}
    return post
