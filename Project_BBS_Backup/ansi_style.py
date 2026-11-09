"""Shared palette and escaped HTML rendering for terminal text runs."""

from html import escape
import re

COLOR_CLASS = re.compile(r'[fb][0-7]|hl')
NORMAL = ('#000000', '#990000', '#009900', '#999900', '#000099', '#990099', '#009999', '#999999')
BRIGHT = ('#666666', '#ff6666', '#66ff66', '#ffff66', '#6666ff', '#ff66ff', '#66ffff', '#ffffff')
BACKGROUND = ('#000000', '#bb0000', '#00bb00', '#bbbb00', '#0000bb', '#bb00bb', '#00bbbb', '#bbbbbb')

ANSI_STYLE = '''
.color-reference { margin: 0; padding: .03em 0 1em; color: #999; background: #000;
    font: 20px/.96 "MingLiU", "PMingLiU", "細明體", monospace;
    font-variant-ligatures: none; white-space: pre; overflow-x: auto; tab-size: 8; }
.ansi-run { display: inline-block; vertical-align: top; height: .95em; line-height: .95; }
.block-cell { display: inline-block; position: relative; vertical-align: top;
    width: 1em; height: .95em; overflow: hidden; text-indent: -100em; }
.block-cell::after { content: ''; position: absolute; inset: 0;
    background: linear-gradient(to right, transparent 0 1px, currentColor 1px calc(100% - 1px),
                                transparent calc(100% - 1px)); }
.f0 .block-cell::after { background: currentColor; }
'''
ANSI_STYLE += ''.join(f'.f{i}{{color:{color}}}.b{i}{{background-color:{BACKGROUND[i]}}}'
                      for i, color in enumerate(NORMAL))
ANSI_STYLE += ''.join(f'.f{i}.hl{{color:{color}}}' for i, color in enumerate(BRIGHT))


def render_run(text, classes):
    # The SDK can retain backspace reservation markers used for wide cells.
    value = escape(text.replace('\x08', ''))
    classes = [token for token in classes if COLOR_CLASS.fullmatch(token)]
    if not classes:
        return value
    if any(token.startswith('f') for token in classes):
        value = value.replace('█', '<span class="block-cell">█</span>')
    class_names = ' '.join(classes)
    return '\n'.join(f'<span class="ansi-run {class_names}">{line}</span>' if line else ''
                     for line in value.split('\n'))


METADATA_STYLE = '''
.article-meta { color: #999; background: #008; margin: 0; display: grid;
    font: 20px/19px "MingLiU", "PMingLiU", "細明體", monospace; }
.article-meta > div { display: flex; min-width: 0; }
.article-meta dt { background: #999; color: #008; padding-right: .35em; }
.article-meta dd { margin: 0 0 0 .35em; white-space: pre-wrap; }
.article-meta .board { margin-left: auto; }
'''


def render_metadata(author, title, date, board):
    rows = []
    for label, value in (('作者', author), ('標題', title), ('時間', date)):
        if not value:
            continue
        fields = f'<dt>{label}</dt><dd>{escape(value)}</dd>'
        if label == '作者' and board:
            fields += f'<dt class="board">看板</dt><dd>{escape(board)}</dd>'
        rows.append(f'<div>{fields}</div>')
    if board and not author:
        rows.append(f'<div><dt>看板</dt><dd>{escape(board)}</dd></div>')
    return '<dl class="article-meta">' + ''.join(rows) + '</dl>'
