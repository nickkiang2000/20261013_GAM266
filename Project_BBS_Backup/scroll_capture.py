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


def painted_key(row):
    from itertools import groupby
    text, runs = row
    if not runs: return row
    cells=[]
    for value, style in runs:
        for char in value:
            cells.append((char, () if char==' ' and not any(token.startswith('b') for token in style) else style))
    if ''.join(char for char,_ in cells) != text: return row
    normalized=[]
    for style, group in groupby(cells,key=lambda cell:cell[1]):
        normalized.append((''.join(char for char,_ in group),style))
    return text, tuple(normalized) if any(style for _,style in normalized) else ()


def edge_extension(a, b, *, allow_background=False):
    """Choose an observed longer row, never deleting painted background cells."""
    def parts(row):
        text,runs=row
        left=len(text)-len(text.lstrip(' '));right=len(text)-len(text.rstrip(' '))
        stop=len(text)-right if right else len(text)
        styles=[style for value,style in runs for _ in value] if runs else [()]*len(text)
        if len(styles)!=len(text):return None
        core=(text[left:stop],tuple((text[i],styles[i]) for i in range(left,stop)))
        return left,right,core,styles
    x,y=parts(a),parts(b)
    if x is None or y is None or x[2]!=y[2]:return None
    if x[0]<=y[0] and x[1]<=y[1]:short,long,small,large=a,b,x,y
    elif y[0]<=x[0] and y[1]<=x[1]:short,long,small,large=b,a,y,x
    else:return None
    if short[0]==long[0]:return None
    # Added cells must be ordinary spacing. Keep all raw observations in diagnostics.
    indices=list(range(large[0]-small[0]))
    if large[1]>small[1]:indices+=list(range(len(long[0])-(large[1]-small[1]),len(long[0])))
    if not allow_background and any(any(token.startswith('b') for token in large[3][i]) for i in indices):return None
    return long


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
        self.edge_votes = {}
        self.edge_repaired_rows = set()
        self.viewport_edge_reused_rows = set()
        self.confirmed_repaint_rows = set()
        self.blank_source_advances = 0

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
        previous = [painted_key(self.row_key(row)) for row in self.previous]
        observed = [painted_key(self.row_key(row)) for row in current]
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

    def source_advance_seam(self, current, first, last):
        # Three already connected windows establish the source/physical relation.
        # Only established, unpainted empty lines may advance without text anchors.
        if (self.revision < 3 or self.last_range is None or first is None or last is None): return []
        advance = first-self.last_range[0]
        if advance <= 0 or last-self.last_range[1] != advance: return []
        if current == self.previous:
            # An established run of truly empty lines is still article content.
            # Painted blanks and repeated nonempty art do not take this path.
            if (len(self.offsets) >= 2 and self.offsets[-1] == self.offsets[-2]
                    and 0 < advance <= len(current)-2
                    and all(padding(row) for row in current)):
                self.blank_source_advances += advance
                return [(len(current)-advance, 0)]
            return []
        before=[painted_key(self.row_key(row)) for row in self.previous]
        after=[painted_key(self.row_key(row)) for row in current]
        candidates=[]
        for overlap in range(2,min(len(before),len(after))):
            removed=len(before)-overlap
            lines,pending=self.fold(self.previous[:removed])
            if not pending and len(lines)==advance and before[-overlap:]==after[:overlap]:
                candidates.append((overlap,0))
        return candidates if len(candidates)==1 else []

    def repair_observed_edges(self, current, first, last):
        # Require two distinct redraw positions plus unchanged neighbouring anchors.
        if (self.last_range is None or first is None or last is None or self.revision<3):return False
        advance=first-self.last_range[0]
        if advance<=0 or last-self.last_range[1]!=advance:return False
        size=len(self.previous)-advance
        if not 2<=size<=len(current):return False
        before=self.previous[-size:];after=current[:size]
        if any(wrap_prefix(row) for row in (*before,*after)):return False
        differences=[];anchors=set()
        for i,(old,new) in enumerate(zip(before,after)):
            if painted_key(self.row_key(old))==painted_key(self.row_key(new)):
                if old[0].strip():anchors.add(old[0])
            else:
                chosen=edge_extension(old,new)
                if chosen is None:return False
                differences.append((i,chosen))
        if not differences or len(differences)>2 or len(anchors)<2:return False
        if len(differences)==1 and differences[0][0]==0:
            # The top row may be repainted with different edge spacing. It is
            # already stored from the preceding full view; preserve that row.
            # All other overlap rows and at least two anchors still agree.
            current[0]=before[0]
            self.viewport_edge_reused_rows.add(first)
            return True
        ready=[]
        for i,chosen in differences:
            number=first+i;key=(number,chosen)
            self.edge_votes.setdefault(key,set()).add(first)
            if len(self.edge_votes[key])>=2:ready.append((i,chosen,number))
        if len(ready)!=len(differences):return False
        for i,chosen,number in ready:
            previous_index=advance+i
            absolute=len(self.rows)-len(self.previous)+previous_index
            self.rows[absolute]=chosen
            self.previous[previous_index]=chosen
            current[i]=chosen
            self.edge_repaired_rows.add(number)
        return True

    def confirmed_repaint_seam(self, current, first, last):
        """Retain an observed painted edge, or confirm a first-screen blank redraw.

        Two different source positions and at least three exact text/color anchors
        must identify the same row. Never replace a nonblank glyph or its color.
        """
        if self.last_range is None or first is None or last is None: return []
        advance = first-self.last_range[0]
        if advance <= 0: return []
        initial = self.revision == 1 and self.last_range[0] == 1
        if not initial and (self.revision < 3 or last-self.last_range[1] != advance): return []
        candidates = []
        for removed in range(1, len(self.previous)-1):
            overlap = len(self.previous)-removed
            if overlap > len(current): continue
            prefix, pending = self.fold(self.previous[:removed])
            if pending or not (len(prefix) == advance or (initial and len(prefix) == advance+1)): continue
            before, after = self.previous[removed:], current[:overlap]
            if any(wrap_prefix(row) for row in (*before, *after)): continue
            changes, anchors = [], set()
            for i, (old, new) in enumerate(zip(before, after)):
                if painted_key(self.row_key(old)) == painted_key(self.row_key(new)):
                    if old[0].strip(): anchors.add(old[0])
                    continue
                chosen = edge_extension(old, new, allow_background=True)
                if chosen is None and initial and padding(old) and new[0].strip(): chosen = new
                if chosen is None: break
                changes.append((i, chosen))
            else:
                if 0 < len(changes) <= 2 and len(anchors) >= 3:
                    candidates.append((removed, overlap, changes))
        if len(candidates) != 1: return []
        removed, overlap, changes = candidates[0]
        ready = []
        for i, chosen in changes:
            absolute = len(self.rows)-len(self.previous)+removed+i
            key = ('repaint', absolute, chosen)
            self.edge_votes.setdefault(key, set()).add(first)
            if len(self.edge_votes[key]) >= 2: ready.append((i, chosen, absolute))
        if len(ready) != len(changes): return []
        for i, chosen, absolute in ready:
            self.rows[absolute] = chosen
            self.previous[removed+i] = chosen
            current[i] = chosen
            self.confirmed_repaint_rows.add(absolute+1)
        return [(overlap, 0)]

    def record_failure(self, current, first, last, reason):
        snapshot = {'accepted': list(self.previous), 'observed': list(current),
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
            if not choices and self.repair_observed_edges(current, first, last):
                choices = self.seam(current)
            candidates = [size for size, _ in choices]
            choices, resolved = self.resolve_ambiguity(choices, first)
            if not choices:
                choices = self.confirmed_repaint_seam(current, first, last)
            if not choices:
                choices = self.source_advance_seam(current, first, last)
                resolved = bool(choices)
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
                'edge_repaired_rows': sorted(self.edge_repaired_rows),
                'viewport_edge_reused_rows': sorted(self.viewport_edge_reused_rows),
                'confirmed_repaint_rows': sorted(self.confirmed_repaint_rows),
                'blank_source_advances': self.blank_source_advances,
                'accepted_display_rows': self.rows if self.error and self.end is not None else None}
