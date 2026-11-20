"""Portable offline archive pages. All links work directly from disk."""

from hashlib import sha256
from html import escape
from itertools import groupby
import json
import re

from terminal_links import WEB_URL
from issue_records import report as issue_report
import issue_records
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
OFFLINE_README = Path(__file__).with_name('offline_readme.html')
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
    currentPage: 0,
    position: -1,
}));
let activeBoard = null;

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
        entry.addEventListener('focus', () => select(board, index, false));
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
    if (board) select(board, board.entries.indexOf(returning));
    else if (boards.length) select(boards[0], boards[0].entries.length - 1);
}
restoreSelection();
window.addEventListener('hashchange', restoreSelection);
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

ARTICLE_KEYS = '''
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
        location.href = document.getElementById('back').href + '/' + mode;
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
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_bytes(data)
    temp.replace(path)
    return True


def file_stamp(path):
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except FileNotFoundError:
        return None


def linkified_line(line, runs=(), targets=None):
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
        target = (targets or {}).get(url, url)
        if not isinstance(target, str) or not WEB_URL.fullmatch(target):
            target = url
        parts.append(f'<a href="{escape(target, quote=True)}" target="_blank" rel="noopener noreferrer">'
                     + render_slice(match.start(), stop) + '</a>')
        cursor = stop
    return ''.join(parts) + render_slice(cursor, len(line))


def article_body(body, color_lines, *, start_line=0, targets=None):
    targets = targets if isinstance(targets, dict) else {}
    lines = body.split('\n')
    end = len(lines)
    while end > start_line and not lines[end - 1].strip(' \t\r\x08'):
        runs = (color_lines or {}).get(str(end - 1), ())
        if any(any(token in {'b1', 'b2', 'b3', 'b4', 'b5', 'b6', 'b7'} for token in classes)
               for _, classes in runs):
            break
        end -= 1
    # Keep a short final frame aligned to the article's page boundary.
    frame_style = f' style="--frame-rows: {FRAME_ROWS}; --frame-padding: {(-(end - start_line)) % FRAME_ROWS}lh"'
    if color_lines is None:
        return f'<pre{frame_style}>\n' + '\n'.join((linkified_line(line, targets=targets) for line in lines[:end])) + '</pre>'
    rendered = []
    for index in range(start_line, end):
        line = lines[index]
        runs = color_lines.get(str(index), [])
        if runs and ''.join(text for text, _ in runs) == line:
            rendered.append(linkified_line(line, runs, targets))
        else:
            rendered.append(linkified_line(line, targets=targets))
    return f'<style>{ANSI_STYLE}</style><pre class="color-reference"{frame_style}>\n' + '\n'.join(rendered) + '</pre>'


COLOR_ARTICLE_STYLE = '''
html:has(.ansi-article) { background: #000; }
body.ansi-article { background: #000; margin: 0 auto; padding: 0; }
.ansi-article .article-top { background: #0c0a66; color: #fff; min-height: 38px;
    display: flex; align-items: baseline; flex-wrap: wrap; gap: .75em; padding: 4px .5em 0;
    box-sizing: border-box; font: 20px/19px "MingLiU", "PMingLiU", "細明體", monospace; }
.article-top .brand { color: #ff6; }
.article-top nav { margin-left: auto; gap: .75em; font-size: 14px; }
'''

INDEX_STYLE = '''
body:has(.index-top) { margin: 0 auto; padding: 0; background: #000; }
html:has(.index-top) { background: #000; }
.index-top { display: flex; gap: .6rem; align-items: baseline; flex-wrap: wrap;
    background: #0c0a66; padding: .4rem .65rem; }
.index-top h1 { color: #ffff66; margin: 0; font: 1.3rem/1.3 "MingLiU", "細明體", monospace; }
.index-top a { margin-left: auto; font-size: .85rem; }
.board-tools { display: flex; flex-wrap: wrap; align-items: center; gap: .35rem .6rem;
    padding: .22rem .55rem; border-bottom: 1px solid #4a4a4a; }
.board-tab { background: #b5b5b5; color: #000; padding: .08rem .55rem; }
.board-tools .pagination { margin-left: auto; gap: .12rem; }
.board-tools button { background: #3b3b3b; color: #eee; border: 1px solid #676767; padding: .08rem .45rem; }
.board-tools button:disabled { background: #1e1e1e; color: #8b8b8b; }
.board-tools .page-label { padding: 0 .25rem; }
.board .entries { margin: .15rem 0; }
.board .entries a { padding: .1rem .65rem; font-size: .92rem; line-height: 1.35; }
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
    return re.sub(r'^(?:\s*re\s*:\s*)+', '', title, flags=re.I).strip().lower()


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
    return (f'<style>{COLOR_ARTICLE_STYLE}{METADATA_STYLE}</style>' + header + metadata
            + article_body(body, post.get('ansi_lines', {}), start_line=4 if has_terminal_header else 0,
                           targets=post.get('link_targets')))


def export_html(db, folder, progress=None, *, force=False, stats=None, issue_evidence=None):
    articles = folder / 'articles'
    articles.mkdir(exist_ok=True)
    db.execute('''CREATE TABLE IF NOT EXISTS html_render_cache(
        path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, size INTEGER NOT NULL, modified INTEGER NOT NULL)''')
    cached = {path: (digest, (size, modified)) for path, digest, size, modified in db.execute(
        'SELECT path,fingerprint,size,modified FROM html_render_cache')}
    cache_updates = {}
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
        for i, (_, _, aid, title, body, data, source_index, _) in enumerate(items):
            key = keys[i]
            title = title or '(無標題)'
            label = f'{source_index} · ' if source_index is not None else ''
            render_error = None
            try:
                post = json.loads(data) if data is not None else {}
                if not isinstance(post, dict):
                    raise ValueError('Article data must be an object')
            except (ValueError, TypeError) as exc:
                post = {}
                render_error = type(exc).__name__
            author = post.get('author') or ''
            if post.get('manual_ansi'):
                label += '人工備份 · '
            if data is not None:
                search_rows.append((key, source_index, aid, title, str(author), body, post.get('comments')))
            label += f'{author} · ' if author else ''
            neighbors = tuple(keys[target] if target is not None else None
                              for target in subject_links.get(i, (None, None)))
            subject_attrs = ''.join(f' data-subject-{direction}="a-{neighbor}"'
                                    for direction, neighbor in zip(('older', 'newer'), neighbors) if neighbor)
            index_attr = f' data-source-index="{source_index}"' if type(source_index) is int and source_index > 0 else ''
            entries.append(f'<li><a id="a-{key}" href="articles/{key}.html"{index_attr}{subject_attrs}>'
                           f'<span class="meta">{escape(label)}</span>{escape(title)}</a></li>')
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
                               f'{escape(site)} / {escape(board)} · {escape(aid)}</p>' + article_body(body, None, targets=post.get('link_targets')))
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
        pages = []
        for i, batch in enumerate(batches):
            hidden = ' hidden' if i < len(batches) - 1 else ''
            pages.append(f'<ul class="entries"{hidden}>' + ''.join(batch) + '</ul>')
        pagination = ('<nav class="pagination" aria-label="看板翻頁">'
                      '<button class="oldest" type="button">最舊</button>'
                      '<button class="older" type="button">‹ 上頁</button>'
                      '<button class="newer" type="button" disabled>下頁 ›</button>'
                      '<button class="latest" type="button" disabled>最新</button>'
                      f'<span class="page-label meta" aria-live="polite">第 {len(batches)}／{len(batches)} 頁</span></nav>')
        board_name = escape(f'{site} / {board}', quote=True)
        article_count = sum(item[5] is not None for item in items)
        skipped_count = len(items) - article_count
        board_summaries.append(f'<li>{board_name} · {article_count} 篇正文</li>')
        board_count = f'{article_count} 篇' + (f' · {skipped_count} 筆站上狀態' if skipped_count else '')
        sections.append(f'<section class="board" data-board="{board_name}">'
                        '<div class="board-tools"><strong class="board-tab">看板</strong>'
                        f'<span class="meta">{board_name} · {board_count}</span>'
                        + pagination + '</div>' + ''.join(pages) + '</section>')
    index = (f'<style>{INDEX_STYLE}</style><header class="index-top">'
             '<h1>BBS時光庫</h1><span>› <span id="active-board"></span></span>'
             '<button id="open-search" type="button">全板搜尋 ?</button>'
             '<a href="ReadMe.html">操作說明與紀錄</a></header>'
             '<output id="number-jump" class="meta" aria-live="polite"></output>'
             + (''.join(sections) if sections else '<p>尚無文章</p>') + SEARCH_DIALOG)
    readme = OFFLINE_README.read_text(encoding='utf-8')
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
    write_page(folder / 'index.html', page('BBS時光庫', index, index_keys), force=force)
    if cache_updates:
        with db:
            db.executemany('INSERT OR REPLACE INTO html_render_cache VALUES(?,?,?,?)',
                           ((path, *values) for path, values in cache_updates.items()))
    if stats is not None:
        stats['written'] = written
        stats['rendered'] = rendered
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
