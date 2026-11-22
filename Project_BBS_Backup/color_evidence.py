"""Reconcile only omitted SGR components at an already-verified source line."""
from itertools import groupby

FG, BG, BRIGHT = 1, 2, 4
ALL = FG | BG | BRIGHT


def components(style):
    return (next((s for s in style if s.startswith('f')), None),
            next((s for s in style if s.startswith('b')), None), 'hl' in style)


def expand(line):
    output = [tuple(style) for text, style in line.color_runs for _ in text]
    if not line.color_runs:
        return [()] * len(line)
    return output if len(output) == len(line) else None


def reconcile(previous, current):
    if str(previous) != str(current):
        return None
    old_styles, new_styles = expand(previous), expand(current)
    if old_styles is None or new_styles is None:
        return None
    old_masks = getattr(previous, 'color_mask', (ALL,) * len(previous))
    new_masks = getattr(current, 'color_mask', (ALL,) * len(current))
    if len(old_masks) != len(previous) or len(new_masks) != len(current):
        return None
    if previous.color_runs == current.color_runs:
        return previous.color_runs, tuple(a | b for a, b in zip(old_masks, new_masks))
    if all(mask == ALL for mask in (*old_masks, *new_masks)):
        return None  # Preserve the existing strict behavior without omitted SGR evidence.
    styles, masks = [], []
    for old, new, left, right in zip(old_styles, new_styles, old_masks, new_masks):
        a, b = components(old), components(new)
        merged = []
        for index, bit in enumerate((FG, BG, BRIGHT)):
            # Default foreground and explicit normal white are visually equal.
            same = (a[index] or 'f7') == (b[index] or 'f7') if bit == FG else a[index] == b[index]
            if left & right & bit and not same:
                return None
            if not (left | right) & bit and not same:
                return None
            merged.append(a[index] if left & bit or not right & bit else b[index])
        fg, bg, bright = merged
        styles.append(tuple(x for x in (fg, bg, 'hl' if bright else None) if x))
        masks.append(left | right)
    runs = []
    position = 0
    for style, group in groupby(styles):
        length = sum(1 for _ in group)
        runs.append((str(previous)[position:position+length], style))
        position += length
    return tuple(runs), tuple(masks)
