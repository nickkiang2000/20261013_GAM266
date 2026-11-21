"""Bind hyperlink targets to their painted position, then to a unique body block."""
from collections import defaultdict
from difflib import SequenceMatcher
from PyPtt import screens


def frame_links(data, encoding, height, occurrences, rows):
    painted = {}
    for event in occurrences:
        prefix = screens.VT100Parser(data[:event['byte_start']], encoding, height)
        row = prefix._cursor_y
        if not 0 <= row < len(rows):
            continue
        start = screens._str_pos_at_cells(rows[row], prefix._cursor_x)
        label = event['label']
        if rows[row][start:start+len(label)] != label:
            continue
        # Within a redraw, the latest paint at a cell is the visible target.
        painted[row, start, label] = {'row': row, 'start': start, 'label': label, 'target': event['target']}
    return list(painted.values())


def resolve_links(body, frames):
    body_rows = [row.rstrip('\x08') for row in body.split('\n')]
    candidates = defaultdict(set)
    for frame in frames:
        rows = [row.rstrip('\x08') for row in frame['rows']]
        blocks = SequenceMatcher(None, body_rows, rows, autojunk=False).get_matching_blocks()
        for link in frame['links']:
            for block in blocks:
                if not block.b <= link['row'] < block.b + block.size or block.size < 3:
                    continue
                pattern = rows[block.b:block.b+block.size]
                anchors = {row for row in pattern if row.strip() and link['label'] not in row}
                if len(anchors) < 2:
                    continue
                positions = [i for i, row in enumerate(body_rows) if row == pattern[0]
                             and body_rows[i:i+len(pattern)] == pattern]
                if positions != [block.a]:
                    continue
                line = block.a + link['row'] - block.b
                start = link['start']
                if body_rows[line][start:start+len(link['label'])] == link['label']:
                    candidates[line, start, link['label']].add(link['target'])
    return [{'line': line, 'start': start, 'label': label, 'target': next(iter(targets))}
            for (line, start, label), targets in sorted(candidates.items()) if len(targets) == 1]
