"""Portable offline archive pages. All links work directly from disk."""

from hashlib import sha256
from html import escape
from itertools import groupby
import json
import re
import os
import tempfile
from contextlib import contextmanager

from terminal_links import WEB_URL
from issue_records import report as issue_report
import issue_records
import board_metadata
from article_metadata import list_cells, posting_date
from pathlib import Path

from ansi_style import ANSI_STYLE, METADATA_STYLE, render_metadata, render_run


STYLE = '''
body { color-scheme: dark; max-width: 960px; margin: 2rem auto; padding: 0 1rem;
       background: #10151c; color: #e0e7ef; font: 1rem/1.6 system-ui; }
a { color: #a8d7ff; }
.entries { list-style: none; padding: 0; }
.entries a { display: block; padding: .35rem .7rem; text-decoration: none; }
.entries a:hover, .entries a:focus, .entries a[aria-current="true"] {
    background: #253a50; outline: 2px solid #8ac7ff;
}
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #080c11; padding: 1rem;
      font: 1rem/1.55 Consolas, "Microsoft JhengHei", monospace; }
pre.frame-view { max-height: calc(var(--frame-rows) * 1lh); padding-block: 0; overflow: auto; white-space: pre; }
pre.frame-view::after { content: ''; display: block; height: var(--frame-padding); }
pre a, .color-reference a .ansi-run { color: #4da3ff; }
pre a { text-decoration: underline; text-underline-offset: .1em; }
nav { display: flex; flex-wrap: wrap; gap: 1.25rem; }
.pagination { align-items: center; gap: .75rem; }
.pagination button { font: inherit; padding: .25rem .65rem; }
.meta { color: #b2bfd0; font-size: .88rem; }
'''

PAGE_SIZE = 20
FRAME_ROWS = 22
OFFLINE_README = Path(__file__).with_name('archive-guide.template.html')
OFFLINE_SEARCH = Path(__file__).with_name('offline_search.js')
URL_PATTERN = re.compile(r"""https?://[^\s<>"'\x00-\x1f，。！？；：、（）【】《》「」『』]+""", re.I)

INDEX_KEYS = r'''
const boards = Array.from(document.querySelectorAll('.board'), section => ({
    pages: Array.from(section.querySelectorAll('.entries')),
    entries: Array.from(section.querySelectorAll('.entries a')),
    older: section.querySelector('.older'),
    newer: section.querySelector('.newer'),
    oldest: section.querySelector('.oldest'),
    latest: section.querySelector('.latest'),
    label: section.querySelector('.page-label'),
    name: section.dataset.board,
    title: section.dataset.title || '',
    currentPage: 0,
    position: -1,
}));
let activeBoard = null;
function highlightSubject(board, entry) {
    for (const section of boards) for (const row of section.entries) {
        row.classList.remove('thread-root', 'thread-reply', 'thread-related');
    }
    if (!entry?.dataset.subject) return;
    for (const row of board.entries) if (row.dataset.subject === entry.dataset.subject) {
        row.classList.add('thread-related', row.dataset.reply === 'true' ? 'thread-reply' : 'thread-root');
    }
}


function showPage(board, page) {
    board.pages[board.currentPage].hidden = true;
    board.currentPage = page;
    board.pages[page].hidden = false;
    board.older.disabled = page === 0;
    board.newer.disabled = page === board.pages.length - 1;
    board.oldest.disabled = page === 0;
    board.latest.disabled = page === board.pages.length - 1;
    board.label.textContent = `第 ${page + 1}／${board.pages.length} 頁`;
}

function select(board, index, focus = true) {
    if (activeBoard?.position >= 0) {
        activeBoard.entries[activeBoard.position].removeAttribute('aria-current');
    }
    activeBoard = board;
    document.getElementById('active-board').textContent = board.name;
    document.getElementById('board-title').textContent = board.title;
    document.title = 'BBS時光庫 ' + board.name;
    board.position = index;
    const entry = board.entries[index];
    showPage(board, board.pages.indexOf(entry.closest('.entries')));
    entry.setAttribute('aria-current', 'true');
    if (focus) entry.focus();
}

function turnPage(board, step) {
    const page = board.currentPage + step;
    if (page < 0 || page >= board.pages.length) return;
    const entries = board.pages[page].querySelectorAll('a');
    const entry = step < 0 ? entries[entries.length - 1] : entries[0];
    select(board, board.entries.indexOf(entry));
}

for (const board of boards) {
    board.entries.forEach((entry, index) => {
        if (!entry.dataset.subject) {
            const original = board.entries.find(row => row.dataset.subject && row.getAttribute('href')?.split('#')[0] === entry.getAttribute('href')?.split('#')[0]);
            if (original) { entry.dataset.subject = original.dataset.subject; entry.dataset.reply = original.dataset.reply; }
        }
        entry.addEventListener('focus', () => select(board, index, false));
        entry.addEventListener('click', event => {
            if (event.defaultPrevented || event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
            try { history.replaceState(null, '', '#' + entry.id + '/thread'); } catch (_) {}
        });
    });
    board.older.onclick = () => turnPage(board, -1);
    board.newer.onclick = () => turnPage(board, 1);
    board.oldest.onclick = () => select(board, 0);
    board.latest.onclick = () => select(board, board.entries.length - 1);
    showPage(board, board.pages.length - 1);
}

function restoreSelection() {
    const returning = document.getElementById(location.hash.slice(1).split('/')[0]);
    const board = boards.find(candidate => candidate.entries.includes(returning));
    if (board) {
        select(board, board.entries.indexOf(returning));
        if (location.hash.endsWith('/thread')) highlightSubject(board, returning);
    }
    else if (boards.length) select(boards[0], boards[0].entries.length - 1);
}
restoreSelection();
window.addEventListener('hashchange', restoreSelection);
window.addEventListener('pageshow', restoreSelection);
let numberInput = '';
const numberStatus = document.getElementById('number-jump');

document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey ||
        event.isComposing || !activeBoard || document.getElementById('search-dialog').open ||
        event.target.closest?.('input, textarea, select, [contenteditable="true"]')) return;
    const board = activeBoard;
    if (/^\d$/.test(event.key)) {
        event.preventDefault();
        numberInput = (numberInput + event.key).slice(0, 10);
        numberStatus.textContent = `篇號 ${numberInput} · 按 Enter 定位`;
        return;
    }
    if (numberInput) {
        if (event.key === 'Backspace') {
            event.preventDefault();
            numberInput = numberInput.slice(0, -1);
            numberStatus.textContent = numberInput ? `篇號 ${numberInput} · 按 Enter 定位` : '';
            return;
        }
        const number = Number(numberInput);
        numberInput = '';
        numberStatus.textContent = '';
        if (event.key === 'Enter') {
            event.preventDefault();
            const position = board.entries.findIndex(entry => entry.dataset.sourceIndex === String(number));
            if (position >= 0) select(board, position);
            else numberStatus.textContent = `離線索引沒有第 ${number} 篇`;
            return;
        }
        if (event.key === 'Escape') {
            event.preventDefault();
            return;
        }
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        const index = Math.min(board.entries.length - 1, Math.max(0, board.position + step));
        select(board, index);
    } else if (event.key === 'PageUp' || event.key === 'PageDown') {
        event.preventDefault();
        turnPage(board, event.key === 'PageUp' ? -1 : 1);
    } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault();
        select(board, event.key === 'Home' ? 0 : board.entries.length - 1);
    } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        board.entries[board.position].click();
    } else if (event.key === '[' || event.key === ']') {
        event.preventDefault();
        const current = board.entries[board.position];
        const target = event.key === '[' ? current.dataset.subjectOlder : current.dataset.subjectNewer;
        const position = board.entries.findIndex(entry => entry.id === target);
        if (position >= 0) select(board, position);
    }
});
'''

ARTICLE_KEYS = r'''
// Mark the topic only after the article page has actually opened.
const topicBack = document.getElementById('back');
if (topicBack && !topicBack.hash.endsWith('/thread')) topicBack.hash += '/thread';

if (location.hash === '#sticky') {
    const key = location.pathname.split('/').pop().replace(/\.html$/, '');
    if (/^[0-9a-f]{64}$/.test(key)) document.getElementById('back').hash = 'pin-' + key + '/thread';
}
const articleText = document.querySelector('pre');
const frameButton = document.getElementById('frame-view');
const frameRows = Number(articleText.style.getPropertyValue('--frame-rows'));
let movedDown = false;
const articleViewport = () => articleText.classList.contains('frame-view')
    ? articleText : document.scrollingElement;
const trackScroll = () => { if (articleViewport().scrollTop > 1) movedDown = true; };
articleText.addEventListener('scroll', trackScroll);
window.addEventListener('scroll', trackScroll);
frameButton.addEventListener('click', () => {
    const enabled = articleText.classList.toggle('frame-view');
    frameButton.setAttribute('aria-pressed', String(enabled));
    frameButton.textContent = `${enabled ? '離開 ' : ''}${frameRows} 行視窗`;
    articleText.scrollTop = 0;
    document.scrollingElement.scrollTop = 0;
});
document.addEventListener('keydown', event => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.isComposing) return;
    const key = event.key;
    const shortcut = {h: 'help', H: 'help', '[': 'subject-older', ']': 'subject-newer'}[key];
    if (shortcut) {
        const link = document.getElementById(shortcut);
        if (link) {
            event.preventDefault();
            link.click();
        }
        return;
    }
    const mode = {'?': 'all', '/': 'title', a: 'author', '#': 'aid', S: 'subject', '!': 'exclude'}[key];
    if (mode) {
        event.preventDefault();
        location.href = document.getElementById('back').href.replace(/\/thread$/, '') + '/' + mode + '/thread';
        return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
        event.preventDefault();
        const viewport = articleViewport();
        const remaining = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop;
        const pageForward = event.key === 'ArrowRight';
        if (remaining > 1) {
            const lineHeight = parseFloat(getComputedStyle(articleText).lineHeight);
            const distance = pageForward ? lineHeight * frameRows : lineHeight;
            movedDown = true;
            viewport.scrollTop += Math.min(distance, remaining);
        } else {
            document.getElementById(pageForward ? 'back' : 'next')?.click();
        }
        return;
    }
    if (event.key === 'ArrowUp' && (movedDown || articleViewport().scrollTop > 1)) {
        event.preventDefault();
        articleViewport().scrollTop -= parseFloat(getComputedStyle(articleText).lineHeight);
        return;
    }
    const id = {Escape: 'back', ArrowLeft: 'back', ArrowUp: 'previous'}[event.key];
    const link = id && document.getElementById(id);
    if (link) {
        event.preventDefault();
        link.click();
    }
});
'''


def page(title, body, script, *, ansi=False, extra_scripts=''):
    body_class = ' class="ansi-article"' if ansi else ''
    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>{STYLE}</style></head>
<body{body_class}>{body}{extra_scripts}<script>{script}</script></body></html>'''


def write_page(path, content, *, force=False):
    data = content.encode('utf-8')
    if not force:
        try:
            if path.read_bytes().replace(b'\r\n', b'\n') == data:
                return False
        except FileNotFoundError:
            pass
    temp=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix=path.name+'.',suffix='.tmp',delete=False) as stream:
            temp=Path(stream.name);stream.write(data)
        temp.replace(path)
    finally:
        if temp is not None:temp.unlink(missing_ok=True)
    return True


def file_stamp(path):
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except FileNotFoundError:
        return None


def linkified_line(line, runs=(), targets=None, occurrences=()):
    def render_slice(start, stop):
        if not runs:
            return escape(line[start:stop])
        return ''.join(render_run(line[max(start, left):min(stop, right)], classes)
                       for left, right, classes in offsets if start < right and left < stop)

    if not URL_PATTERN.search(line):
        return ''.join(render_run(value, classes) for value, classes in runs) if runs else escape(line)
    offsets = []
    offset = 0
    for value, classes in runs:
        offsets.append((offset, offset + len(value), classes))
        offset += len(value)
    parts = []
    cursor = 0
    for match in URL_PATTERN.finditer(line):
        url = match[0].rstrip('.,!?;:')
        for left, right in (('(', ')'), ('[', ']'), ('{', '}')):
            while url.endswith(right) and url.count(right) > url.count(left):
                url = url[:-1]
        if len(url.partition('://')[2]) == 0:
            continue
        stop = match.start() + len(url)
        parts.append(render_slice(cursor, match.start()))
        # A terminal continuation marker may have survived in an old backup.
        # Use an exact known label; never infer an unknown URL by removing '/'.
        if url.endswith('\\') and url[:-1] in (targets or {}):
            url = url[:-1]
        matching = [link for link in occurrences if link['start'] == match.start()
                    and link['label'] in (url, url.removesuffix('\\'))]
        positional = {link['target'] for link in matching}
        target = next(iter(positional)) if len(positional) == 1 else (targets or {}).get(url, url)
        if not isinstance(target, str) or not WEB_URL.fullmatch(target):
            target = url
        parts.append(f'<a href="{escape(target, quote=True)}" target="_blank" rel="noopener noreferrer">'
                     + (escape(target) if len(positional) == 1 else render_slice(match.start(), stop)) + '</a>')
        cursor = stop
    return ''.join(parts) + render_slice(cursor, len(line))


def occurrence_targets(targets, links):
    local = dict(targets)
    values = {}
    for link in links:
        values.setdefault(link['label'], set()).add(link['target'])
    local.update({label: next(iter(addresses)) for label, addresses in values.items() if len(addresses) == 1})
    return local


def display_lines(lines, colors, targets, start, end, line_links=None):
    """Join a displayed URL only when its saved full target proves both pieces."""
    output = []
    index = start
    while index < end:
        line = lines[index]
        runs = (colors or {}).get(str(index), [])
        joined = False
        if index + 1 < end:
            for label, target in occurrence_targets(targets, (line_links or {}).get(index, [])).items():
                if (not isinstance(target, str) or not WEB_URL.fullmatch(target)
                        or not target.startswith(label) or target == label):
                    continue
                head = line.rstrip(' ')
                tail = lines[index + 1]
                if tail.rstrip(' ') != target[len(label):]:
                    continue
                if head.endswith(label + '\\'):
                    head = head[:-1]
                elif not head.endswith(label):
                    continue
                first_runs = []
                offset = 0
                for text, style in runs:
                    if offset >= len(head):
                        break
                    first_runs.append((text[:len(head)-offset], style))
                    offset += len(text)
                tail_runs = (colors or {}).get(str(index + 1), [])
                if (''.join(text for text, _ in first_runs) == head
                        and ''.join(text for text, _ in tail_runs) == tail):
                    runs = first_runs + list(tail_runs)
                else:
                    runs = []
                line = head + tail
                joined = True
                break
        output.append((index, line, runs, joined))
        index += 2 if joined else 1
    return output


def article_body(body, color_lines, *, start_line=0, targets=None, soft_wrap_lines=(), personalized=False, link_occurrences=()):
    targets = targets if isinstance(targets, dict) else {}
    lines = body.split('\n')
    line_links = {}
    for link in link_occurrences:
        index = link.get('line', -1)
        start = link.get('start', -1)
        if (isinstance(index, int) and isinstance(start, int) and 0 <= index < len(lines) and start >= 0
                and lines[index][start:start+len(link.get('label', ''))] == link.get('label')
                and isinstance(link.get('target'), str) and WEB_URL.fullmatch(link['target'])):
            line_links.setdefault(index, []).append(link)
    end = len(lines)
    while end > start_line and not lines[end - 1].strip(' \t\r\x08'):
        runs = (color_lines or {}).get(str(end - 1), ())
        if any(any(token in {'b1', 'b2', 'b3', 'b4', 'b5', 'b6', 'b7'} for token in classes)
               for _, classes in runs):
            break
        end -= 1
    entries = display_lines(lines, color_lines, targets, start_line if color_lines is not None else 0, end, line_links)
    frame_style = f' style="--frame-rows: {FRAME_ROWS}; --frame-padding: {(-len(entries)) % FRAME_ROWS}lh"'
    if color_lines is None:
        return f'<pre{frame_style}>\n' + '\n'.join(linkified_line(line, targets=occurrence_targets(targets, line_links.get(index, [])), occurrences=line_links.get(index, [])) for index, line, _, _ in entries) + '</pre>'
    rendered = []
    soft_wrap_lines = set(soft_wrap_lines)
    for index, line, runs, joined_url in entries:
        links = line_links.get(index, [])
        local_targets = occurrence_targets(targets, links)
        if personalized:
            neutral = re.sub(r'(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]{1,12}(?=\s+你有什麼特別的願望呢)', '〔讀者〕', line)
            if neutral != line:
                line, runs = neutral, []
        if runs and ''.join(text for text, _ in runs) == line:
            rendered.append(linkified_line(line, runs, local_targets, links))
        else:
            rendered.append(linkified_line(line, targets=local_targets, occurrences=links))
        if index in soft_wrap_lines or joined_url or links:
            rendered[-1] = '<span class="soft-wrap-line">' + rendered[-1] + '</span>'
    return f'<style>{ANSI_STYLE}</style><pre class="color-reference"{frame_style}>\n' + '\n'.join(rendered) + '</pre>'


COLOR_ARTICLE_STYLE = '''
html:has(.ansi-article) { background: #000; }
body.ansi-article { background: #000; margin: 0 auto; padding: 0; }
.ansi-article .soft-wrap-line { display: inline-block; vertical-align: top; width: 100%;
    white-space: pre-wrap; overflow-wrap: anywhere; }
.ansi-article .soft-wrap-line .ansi-run { display: inline; height: auto; line-height: inherit; }
.ansi-article .article-top { background: #0c0a66; color: #fff; min-height: 38px;
    display: flex; align-items: baseline; flex-wrap: wrap; gap: .75em; padding: 4px .5em 0;
    box-sizing: border-box; font: 20px/19px "MingLiU", "PMingLiU", "細明體", monospace; }
.article-top .brand { color: #ff6; }
.article-top nav { margin-left: auto; gap: .75em; font-size: 14px; }
'''

INDEX_STYLE = '''
body:has(.index-top) { margin: 0; padding: 0; background: #000; max-width: none;
    --index-head-height: 40px; --entry-columns: min(37.5%, 58ch) minmax(0,1fr);
    font: 14px/1.65 Consolas, 'Microsoft JhengHei', monospace; }
html:has(.index-top) { background: #000; }
.index-top { display: flex; justify-content: space-between;
    position: sticky; top: 0; z-index: 20; height: var(--index-head-height); align-items: center;
    padding: 0 8px; background: #0c0a66; white-space: nowrap; box-sizing: border-box; }
.index-brand, .index-actions { min-width: 0; }
.index-brand { overflow: hidden; text-overflow: ellipsis; font: 13px/1.5 system-ui; }
.index-actions { text-align: right; font: 11px/1.5 system-ui; }
#board-title { position: absolute; left: 50%; transform: translateX(-50%); color: #ffff00;
    text-align: center; font: 17px/1.3 'MingLiU',serif; max-width: 48%;
    overflow: hidden; text-overflow: ellipsis; }
#number-jump { position: absolute; top: 100%; left: 8px; background: #0c0a66; z-index: 22; }
#number-jump:empty { display: none; }
.sticky-star { color: #ffff00; text-align: center; }
.board-tools, .board .entries a, .board .sticky-missing { display: grid;
    grid-template-columns: var(--entry-columns); padding: 0 8px; }
.board-tools { position: sticky; top: var(--index-head-height); z-index: 19;
    align-items: center; height: 30px; box-sizing: border-box;
    background: #b5b5b5; color: #000; white-space: nowrap; }
.entry-columns, .entry-meta { display: grid; grid-template-columns: 6ch 4ch 14ch minmax(0,1fr);
    padding-right: 4ch; white-space: nowrap; min-width: 0; }
.entry-number, .entry-columns > span:first-child { margin-right: 2ch; }
.entry-push, .entry-columns > span:nth-child(2) { padding-right: 2ch; text-align: right;
    font-variant-numeric: tabular-nums; }
.entry-date, .entry-columns > span:nth-child(3) { padding-right: 4ch; }
.title-tools { display: flex; align-items: center; gap: 4px; min-width: 0; white-space: nowrap; }
.board-tools .pagination { display: flex; margin: 0 0 0 auto; gap: 3px; flex-wrap: nowrap;
    align-items: center; font: 11px/1.5 system-ui; }
.board-tools button { background: #3b3b3b; color: #eee; border: 1px solid #777;
    padding: 1px 3px; font: inherit; white-space: nowrap; }
.board-tools button:disabled { color: #aaa; }
.board-tools .page-label { padding: 0; color: #000; font-size: inherit; white-space: nowrap; }
.board .entries { margin: 0; }
.board .entries a, .board .sticky-missing { padding-top: 4px; padding-bottom: 4px;
    font: inherit; align-items: start; text-align: left; }
.entry-number, .entry-date { font-variant-numeric: tabular-nums; }
.entry-author { min-width: 0; max-width: 30ch; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.entry-title { overflow-wrap: anywhere; min-width: 0; color: #999999; }
.thread-related .entry-author { color: #ffffff; }
.thread-root .entry-title { color: #ff0000; }
.board[data-site="CCNS"] .thread-root .entry-title { color: #00ff00; }
.thread-reply .entry-title { color: #ffff00; }
.board .sticky-missing > small { grid-column: 2; }
.push-positive { color: #00ff00; } .push-hot { color: #ffff00; }
.push-boom { color: #ff5555; } .push-negative { color: #bbb; }
.manual-label { color: #b2bfd0; margin-left: .4em; }
@media (max-width: 740px) { .title-tools > span { display: none; } }
@media (max-width: 580px) {
    #board-title, .index-actions { display: none; }
    .board-tools { grid-template-columns: 0 minmax(0,1fr); }
    .entry-columns { visibility: hidden; }
    .board .entries a, .board .sticky-missing { grid-template-columns: minmax(0,1fr); }
    .entry-meta { padding-right: 0; }
    .board .sticky-missing > small { grid-column: 1; }
}
#search-dialog { box-sizing: border-box; width: min(43rem, 92vw); max-height: 80vh; border: 1px solid #676767;
    background: #10151c; color: #e0e7ef; padding: 1rem; }
#search-dialog[open] { display: flex; flex-direction: column; overflow: hidden; }
#search-dialog::backdrop { background: #000a; }
#search-dialog header { display: flex; justify-content: space-between; align-items: center; gap: .5rem; }
#search-close { font: inherit; line-height: 1; padding: .3rem .55rem; }
#search-dialog form { display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; }
#search-dialog header, #search-form, #search-status { flex-shrink: 0; }
#search-dialog input { flex: 1 1 14rem; min-width: 0; }
#search-scroll { min-height: 0; overflow-y: auto; padding: .15rem; }
#search-results { list-style: none; padding: 0; margin: 0; }
#search-results li { padding: .3rem .55rem; }
#search-results li:has(a[aria-current="true"]) { background: #253a50; outline: 1px solid #8ac7ff; outline-offset: -1px; }
#search-dialog li span { display: block; overflow-wrap: anywhere; }
'''

SEARCH_DIALOG = '''
<dialog id="search-dialog" aria-labelledby="search-label">
<header><label id="search-label" for="search-query">搜尋全板</label>
<button id="search-close" type="button" aria-label="關閉搜尋" title="關閉搜尋">×</button></header>
<form id="search-form"><input id="search-query" type="search" autocomplete="off">
<button type="submit">搜尋</button></form>
<p id="search-status" class="meta" aria-live="polite"></p>
<div id="search-scroll">
<ul id="search-results"></ul><button id="search-more" type="button" hidden>顯示更多</button>
</div>
</dialog>
'''


def subject_key(title):
    return re.sub(r'^(?:\s*(?:re|r)\s*:\s*)+', '', title, flags=re.I).strip().lower()


def color_article(site, board, title, post, body, nav):
    lines = body.split('\n', 4)
    # The terminal repeats the header in its captured screen. Render it in
    # the web-style metadata bar and keep the original text in the database.
    has_terminal_header = (not post.get('terminal_verified') and len(lines) == 5 and all(
        line.lstrip().startswith(label) for line, label in zip(lines, ('作者', '標題', '時間'))
    ) and lines[3].strip() and set(lines[3].strip()) <= set('─━═-'))
    header = (f'<header class="article-top"><span class="brand">{escape(site)}</span>'
              f'<span>› 看板 {escape(board)}</span>{nav}</header>')
    metadata = render_metadata(post.get('author'), title, post.get('date'), board) if has_terminal_header else ''
    manual = post.get('manual_ansi')
    if manual:
        replacements = manual.get('decode_replacements', 0)
        notice = f' · {replacements} 處解碼替代字元' if replacements else ''
        metadata += (f'<p class="meta">人工 ANSI 備份 · {escape(manual["encoding"])}{notice}'
                     f' · 原檔 <code>{escape(manual["original"])}</code></p>')
    if post.get('replaces_manual'):
        original = post['replaces_manual'].get('original', '')
        original_link = (' · <a href="../' + escape(original, quote=True) + '">人工原檔</a>'
                         if re.fullmatch(r'manual-ansi/[0-9a-f]{64}\.ans', original) else '')
        metadata += '<p class="meta">已由程式下載取代人工備份；舊正文保留於資料庫歷史' + original_link + '</p>'
    soft = post.get('soft_wrap_lines', [])
    # v2.24.0 joined only bright-white system markers in this capture mode.
    # A resulting line wider than a physical terminal row is therefore a
    # recoverable join. Manual/SDK captures receive no guessed wrapping.
    if not soft and post.get('ansi_diagnostics', {}).get('assembly') == 'screen_overlap':
        from PyPtt import screens
        soft = [i for i, line in enumerate(body.split('\n')) if screens._cell_len(line) > 80 and '^L' not in line]
    if '^L' in body:
        soft = []  # Fixed-position animation source is never reflowed.
    if post.get('personalized_capture'):
        metadata += '<p class="meta">含閱讀者替換文字；已辨識的稱呼以〔讀者〕顯示，未宣稱取得作者原始模板。</p>'
    return (f'<style>{COLOR_ARTICLE_STYLE}{METADATA_STYLE}</style>' + header + metadata
            + article_body(body, post.get('ansi_lines', {}), start_line=4 if has_terminal_header else 0,
                           targets=post.get('link_targets'), soft_wrap_lines=soft,
                           personalized=post.get('personalized_capture', False),
                           link_occurrences=post.get('link_occurrences', [])))


class ArchiveBusyError(RuntimeError):
    pass


@contextmanager
def archive_lock(folder):
    """An OS-held lock prevents two processes exporting the same archive."""
    lock_dir=Path(__file__).with_name('log')/'archive-locks'
    lock_dir.mkdir(parents=True,exist_ok=True)
    key=sha256(os.path.normcase(str(Path(folder).resolve())).encode()).hexdigest()
    with (lock_dir/(key+'.lock')).open('a+b') as stream:
        stream.seek(0,2)
        if stream.tell()==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise ArchiveBusyError('另一個程式正在更新此備份目錄，請等候完成後再重試') from exc
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream,fcntl.LOCK_UN)


def export_html(db, folder, progress=None, *, force=False, stats=None, issue_evidence=None):
    with archive_lock(folder):
        return _export_html(db,folder,progress,force=force,stats=stats,issue_evidence=issue_evidence)


def _export_html(db, folder, progress=None, *, force=False, stats=None, issue_evidence=None):
    articles = folder / 'articles'
    articles.mkdir(exist_ok=True)
    db.execute('''CREATE TABLE IF NOT EXISTS html_render_cache(
        path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, size INTEGER NOT NULL, modified INTEGER NOT NULL)''')
    cached = {path: (digest, (size, modified)) for path, digest, size, modified in db.execute(
        'SELECT path,fingerprint,size,modified FROM html_render_cache')}
    cache_updates = {}
    date_sources = {}
    date_unknown = []
    renderer = sha256(Path(__file__).read_bytes() + Path(__file__).with_name('ansi_style.py').read_bytes()).hexdigest()

    def unchanged(path, fingerprint):
        return not force and cached.get(path) == (fingerprint, file_stamp(folder / path))

    def remember(path, fingerprint):
        cache_updates[path] = (fingerprint, *file_stamp(folder / path))

    rows = db.execute('''SELECT * FROM (
                            SELECT site,board,aid,title,body,data,source_index,fetched FROM articles
                            UNION ALL
                            SELECT site,board,'!' || idx,
                                   CASE status WHEN 'deleted' THEN '（被刪除）' WHEN 'empty' THEN '（站上無正文）' ELSE '（已鎖定）' END,
                                   coalesce(aid,''),NULL,idx,0 FROM unavailable_articles
                        ) ORDER BY site, board COLLATE NOCASE, board,
                                   source_index IS NOT NULL, source_index ASC, fetched ASC, aid''')
    total = db.execute('SELECT count(*) FROM articles').fetchone()[0]
    count = 0
    written = 0
    rendered = 0
    if progress:
        progress(count, total)
    sections = []
    board_summaries = []
    archive_boards = []
    search_boards = {}
    search_fingerprint = sha256(renderer.encode())
    for (site, board), group in groupby(rows, key=lambda row: row[:2]):
        items = list(group)
        keys = [sha256(json.dumps(row[:3]).encode()).hexdigest() for row in items]
        search_rows = search_boards[f'{site} / {board}'] = []
        subjects = {}
        for i, item in enumerate(items):
            if item[3] and item[5] is not None:
                subjects.setdefault(subject_key(item[3]), []).append(i)
        subject_links = {i: (group[pos - 1] if pos else None,
                             group[pos + 1] if pos + 1 < len(group) else None)
                         for group in subjects.values() for pos, i in enumerate(group)}
        entries = []
        ccns_pins = []
        for i, (_, _, aid, title, body, data, source_index, _) in enumerate(items):
            key = keys[i]
            title = title or '(無標題)'
            render_error = None
            try:
                post = json.loads(data) if data is not None else {}
                if not isinstance(post, dict):
                    raise ValueError('Article data must be an object')
            except (ValueError, TypeError) as exc:
                post = {}
                render_error = type(exc).__name__
            if site == 'CCNS' and post.get('ccns_sticky') is True:
                ccns_pins.append(dict(aid=aid,title=title,source_index=source_index))
            author = post.get('author') or ''
            if data is not None:
                display_date, date_source = posting_date(post, body, site=site, board=board, aid=aid)
                date_sources[date_source] = date_sources.get(date_source, 0) + 1
                if '待查' in display_date:
                    date_unknown.append({'site':site, 'board':board, 'index':source_index, 'aid':aid})
            if data is not None:
                search_rows.append((key, source_index, aid, title, str(author), body, post.get('comments')))
            neighbors = tuple(keys[target] if target is not None else None
                              for target in subject_links.get(i, (None, None)))
            subject_attrs = ''.join(f' data-subject-{direction}="a-{neighbor}"'
                                    for direction, neighbor in zip(('older', 'newer'), neighbors) if neighbor)
            subject_attrs += f' data-subject="{escape(subject_key(title), quote=True)}" data-reply="{str(bool(re.match(r"^\s*(?:re\s*:|r:)", title, re.I))).lower()}"'
            index_attr = f' data-source-index="{source_index}"' if type(source_index) is int and source_index > 0 else ''
            entries.append(f'<li><a id="a-{key}" href="articles/{key}.html"{index_attr}{subject_attrs}>'
                           f'{list_cells(source_index, post, title, body, site=site, board=board, aid=aid)}</a></li>')
            links = [f'<a id="back" href="../index.html#a-{key}">返回列表</a>']
            if i > 0:
                links.append(f'<a id="previous" href="{keys[i-1]}.html">上一篇 ↑</a>')
            if i + 1 < len(keys):
                links.append(f'<a id="next" href="{keys[i+1]}.html">下一篇 ↓</a>')
            links.append(f'<button id="frame-view" type="button" aria-pressed="false">{FRAME_ROWS} 行視窗</button>')
            links.append('<a id="help" href="../ReadMe.html#keyboard" hidden>操作教學</a>')
            for link_id, neighbor in zip(('subject-older', 'subject-newer'), neighbors):
                if neighbor:
                    links.append(f'<a id="{link_id}" href="../index.html#a-{neighbor}" hidden>同主題</a>')
            nav = '<nav>' + ''.join(links) + '</nav>'
            relative_path = f'articles/{key}.html'
            fingerprint = sha256(json.dumps([renderer, title, body, data, nav], ensure_ascii=False).encode()).hexdigest()
            if data is not None:
                search_fingerprint.update(json.dumps([key, source_index, fingerprint]).encode())
            if unchanged(relative_path, fingerprint):
                if data is not None:
                    count += 1
                    if progress:
                        progress(count, total)
                continue
            rendered += 1
            colored = bool(post.get('ansi_lines') or post.get('headerless') or post.get('manual_ansi'))
            try:
                if data is None:
                    article = f'{nav}<h1>{escape(title)}</h1>' + article_body(body or title, None)
                elif colored:
                    article = color_article(site, board, title, post, body, nav)
                else:
                    article = (f'{nav}<h1>{escape(title)}</h1><p class="meta">'
                               f'{escape(site)} / {escape(board)} · {escape(aid)}</p>' + article_body(body, None, targets=post.get('link_targets'), link_occurrences=post.get('link_occurrences', [])))
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                render_error = type(exc).__name__
            if render_error:
                colored = False
                article = (nav + f'<h1>{escape(title)}</h1><p>原格式呈現失敗，以下保留本機正文；'
                           '<a href="../ReadMe.html#issue-records">查看錯漏紀錄</a>。</p>' +
                           article_body(body or '', None))
                with db:
                    issue_records.record(db, site, board, source_index, aid,
                                         '原格式呈現失敗，已改用純文字 HTML：' + render_error, kind='html')
            changed = write_page(folder / relative_path, page(title, article, ARTICLE_KEYS, ansi=colored),
                                 force=force)
            if not render_error:
                remember(relative_path, fingerprint)
            if data is not None and not render_error:
                with db:
                    db.execute('DELETE FROM article_issues WHERE site=? AND board=? AND issue_key=?',
                               (site, board, 'html:' + aid))
            if data is not None:
                count += 1
                written += changed
                if progress:
                    progress(count, total)
        # Keep article numbers in fixed ranges even when the archive has gaps.
        numbered_pages = {}
        unnumbered = []
        for item, entry in zip(items, entries):
            index = item[6]
            if type(index) is int and index > 0:
                numbered_pages.setdefault((index - 1) // PAGE_SIZE, []).append(entry)
            else:
                unnumbered.append(entry)
        batches = [unnumbered[start:start + PAGE_SIZE] for start in range(0, len(unnumbered), PAGE_SIZE)]
        batches.extend(numbered_pages[page] for page in sorted(numbered_pages))
        pins = board_metadata.render_rows(db, site, board, pins=ccns_pins) if site == 'CCNS' else board_metadata.render_rows(db, site, board)
        metadata = board_metadata.load(db, site, board)
        if not batches and pins: batches = [[]]
        pages = []
        for i, batch in enumerate(batches):
            hidden = ' hidden' if i < len(batches) - 1 else ''
            pages.append(f'<ul class="entries"{hidden}>' + ''.join(batch) + (pins if i == len(batches)-1 else '') + '</ul>')
        search_id = ' id="open-search"' if not sections else ''
        pagination = ('<nav class="pagination" aria-label="看板翻頁">'
                      f'<button{search_id} class="open-search" type="button">全板搜尋</button>'
                      '<button class="oldest" type="button">最舊</button>'
                      '<button class="older" type="button">‹ 上頁</button>'
                      '<button class="newer" type="button" disabled>下頁 ›</button>'
                      '<button class="latest" type="button" disabled>最新</button>'
                      f'<span class="page-label meta" aria-live="polite">第 {len(batches)}／{len(batches)} 頁</span></nav>')
        archive_boards.append(f'{site} / {board}')
        board_name = escape(f'{site} / {board}', quote=True)
        article_count = sum(item[5] is not None for item in items)
        skipped_count = len(items) - article_count
        board_summaries.append(f'<li>{board_name} · {article_count} 篇正文</li>')
        board_count = f'{article_count} 篇' + (f' · {skipped_count} 筆站上狀態' if skipped_count else '')
        sections.append(f'<section class="board" data-site="{escape(site, quote=True)}" data-board="{board_name}" data-title="{escape(metadata.get("title",""), quote=True)}">'
                        '<div class="board-tools"><div class="entry-columns" aria-hidden="true">'
                        '<span>編號</span><span>推</span><span>日期</span><span>ID（暱稱）</span></div><div class="title-tools"><span>文章標題</span>'
                        + pagination + '</div></div>' + ''.join(pages) + '</section>')
    index = (f'<style>{INDEX_STYLE}</style><header class="index-top">'
             '<div class="index-brand">BBS時光庫 <span id="active-board"></span></div>'
             '<strong id="board-title" aria-live="polite"></strong><div class="index-actions">'
             '<a href="ReadMe.html">操作說明與紀錄</a></div>'
             '<output id="number-jump" class="meta" aria-live="polite"></output></header>'
             + (''.join(sections) if sections else '<p>尚無文章</p>') + SEARCH_DIALOG)
    archive_name = ('BBS時光庫 ' + '、'.join(archive_boards)) if archive_boards else 'BBS時光庫'
    readme = OFFLINE_README.read_text(encoding='utf-8')
    readme = readme.replace('<title>BBS時光庫－操作說明與紀錄</title>', '<title>' + escape(archive_name) + '－操作說明與紀錄</title>')
    boards = '<ul>' + ''.join(board_summaries) + '</ul>' if board_summaries else '<p>目前尚無離線文章。</p>'
    readme = readme.replace('<!-- BOARD_SUMMARIES -->', boards).replace(
        '<!-- ISSUE_RECORDS -->', issue_report(db, issue_evidence))
    write_page(folder / 'ReadMe.html', readme, force=force)
    search_digest = search_fingerprint.hexdigest()
    if not unchanged('search-data.js', search_digest):
        search_data = {board: [search_entry(*row) for row in rows] for board, rows in search_boards.items()}
        packed = json.dumps(search_data, ensure_ascii=False, separators=(',', ':'))
        write_page(folder / 'search-data.js', 'window.bbsSearchData = ' + packed + ';\n', force=force)
        remember('search-data.js', search_digest)
    index_keys = INDEX_KEYS + '\n' + OFFLINE_SEARCH.read_text(encoding='utf-8')
    write_page(folder / 'index.html', page(archive_name, index, index_keys), force=force)
    if cache_updates:
        with db:
            db.executemany('INSERT OR REPLACE INTO html_render_cache VALUES(?,?,?,?)',
                           ((path, *values) for path, values in cache_updates.items()))
    if stats is not None:
        stats['written'] = written
        stats['rendered'] = rendered
        stats['date_sources'] = date_sources
        stats['date_unknown'] = date_unknown
    return count


def search_entry(key, index, aid, title, author, body, comments):
    body = body or ''
    extra_comments = []
    if isinstance(comments, list):
        for comment in comments:
            if isinstance(comment, dict):
                comment = comment.get('content')
            if isinstance(comment, str) and comment and comment not in body:
                extra_comments.append(comment)
    return {'key': key, 'index': index, 'aid': aid, 'title': title, 'author': author,
            'subject': subject_key(title), 'body': '\n'.join([body.rstrip(), *extra_comments]).replace('\x08', '')}
