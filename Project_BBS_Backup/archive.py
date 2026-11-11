"""Portable offline archive pages. All links work directly from disk."""

from hashlib import sha256
from html import escape
from itertools import groupby
import json
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
nav { display: flex; flex-wrap: wrap; gap: 1.25rem; }
.pagination { align-items: center; gap: .75rem; }
.pagination button { font: inherit; padding: .25rem .65rem; }
.meta { color: #b2bfd0; font-size: .88rem; }
'''

PAGE_SIZE = 20
FRAME_ROWS = 22
OFFLINE_README = Path(__file__).with_name('offline_readme.html')

INDEX_KEYS = '''
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
    const returning = document.getElementById(location.hash.slice(1));
    const board = boards.find(candidate => candidate.entries.includes(returning));
    if (board) select(board, board.entries.indexOf(returning));
    else if (boards.length) select(boards[0], boards[0].entries.length - 1);
}
restoreSelection();
window.addEventListener('hashchange', restoreSelection);

document.addEventListener('keydown', event => {
    if (event.altKey || event.ctrlKey || event.metaKey || !activeBoard) return;
    const board = activeBoard;
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
    }
});
'''

ARTICLE_KEYS = '''
const articleText = document.querySelector('pre');
const frameButton = document.getElementById('frame-view');
const frameRows = Number(articleText.style.getPropertyValue('--frame-rows'));
frameButton.addEventListener('click', () => {
    const enabled = articleText.classList.toggle('frame-view');
    frameButton.setAttribute('aria-pressed', String(enabled));
    frameButton.textContent = `${enabled ? '離開 ' : ''}${frameRows} 行視窗`;
    articleText.scrollTop = 0;
    document.scrollingElement.scrollTop = 0;
});
document.addEventListener('keydown', event => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
        event.preventDefault();
        const viewport = articleText.classList.contains('frame-view') ? articleText : document.scrollingElement;
        const remaining = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop;
        const pageForward = event.key === 'ArrowRight';
        if (remaining > 1) {
            const lineHeight = parseFloat(getComputedStyle(articleText).lineHeight);
            const distance = pageForward ? lineHeight * frameRows : lineHeight;
            viewport.scrollTop += Math.min(distance, remaining);
        } else {
            document.getElementById(pageForward ? 'back' : 'next')?.click();
        }
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


def page(title, body, script, *, ansi=False):
    body_class = ' class="ansi-article"' if ansi else ''
    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>{STYLE}</style></head>
<body{body_class}>{body}<script>{script}</script></body></html>'''


def write_page(path, content, *, force=False):
    data = content.encode('utf-8')
    if not force:
        try:
            if path.read_bytes().replace(b'\r\n', b'\n') == data:
                return False
        except FileNotFoundError:
            pass
    temp = path.with_suffix('.html.tmp')
    temp.write_bytes(data)
    temp.replace(path)
    return True


def article_body(body, color_lines, *, start_line=0):
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
        return f'<pre{frame_style}>\n' + escape('\n'.join(lines[:end])) + '</pre>'
    rendered = []
    for index in range(start_line, end):
        line = lines[index]
        runs = color_lines.get(str(index), [])
        if runs and ''.join(text for text, _ in runs) == line:
            rendered.append(''.join(render_run(text, classes) for text, classes in runs))
        else:
            rendered.append(escape(line))
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
'''


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
    return (f'<style>{COLOR_ARTICLE_STYLE}{METADATA_STYLE}</style>' + header + metadata
            + article_body(body, post.get('ansi_lines', {}), start_line=4 if has_terminal_header else 0))


def export_html(db, folder, progress=None, *, force=False, stats=None):
    articles = folder / 'articles'
    articles.mkdir(exist_ok=True)
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
    if progress:
        progress(count, total)
    sections = []
    board_summaries = []
    for (site, board), group in groupby(rows, key=lambda row: row[:2]):
        items = list(group)
        keys = [sha256(json.dumps(row[:3]).encode()).hexdigest() for row in items]
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
            label += f'{author} · ' if author else ''
            entries.append(f'<li><a id="a-{key}" href="articles/{key}.html">'
                           f'<span class="meta">{escape(label)}</span>{escape(title)}</a></li>')
            links = [f'<a id="back" href="../index.html#a-{key}">返回列表</a>']
            if i > 0:
                links.append(f'<a id="previous" href="{keys[i-1]}.html">上一篇 ↑</a>')
            if i + 1 < len(keys):
                links.append(f'<a id="next" href="{keys[i+1]}.html">下一篇 ↓</a>')
            links.append(f'<button id="frame-view" type="button" aria-pressed="false">{FRAME_ROWS} 行視窗</button>')
            nav = '<nav>' + ''.join(links) + '</nav>'
            colored = bool(post.get('ansi_lines') or post.get('headerless'))
            try:
                if data is None:
                    article = f'{nav}<h1>{escape(title)}</h1>' + article_body(body or title, None)
                elif colored:
                    article = color_article(site, board, title, post, body, nav)
                else:
                    article = (f'{nav}<h1>{escape(title)}</h1><p class="meta">'
                               f'{escape(site)} / {escape(board)} · {escape(aid)}</p>' + article_body(body, None))
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                render_error = type(exc).__name__
            if render_error:
                colored = False
                article = (nav + f'<h1>{escape(title)}</h1><p>原格式呈現失敗，以下保留本機正文；'
                           '<a href="../ReadMe.html#issue-records">查看錯漏紀錄</a>。</p><pre>' +
                           escape(body or '') + '</pre>')
                with db:
                    issue_records.record(db, site, board, source_index, aid,
                                         '原格式呈現失敗，已改用純文字 HTML：' + render_error, kind='html')
            changed = write_page(articles / f'{key}.html', page(title, article, ARTICLE_KEYS, ansi=colored),
                                 force=force)
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
             '<a href="ReadMe.html">操作說明與紀錄</a></header>'
             + (''.join(sections) if sections else '<p>尚無文章</p>'))
    readme = OFFLINE_README.read_text(encoding='utf-8')
    boards = '<ul>' + ''.join(board_summaries) + '</ul>' if board_summaries else '<p>目前尚無離線文章。</p>'
    readme = readme.replace('<!-- BOARD_SUMMARIES -->', boards).replace('<!-- ISSUE_RECORDS -->', issue_report(db))
    write_page(folder / 'ReadMe.html', readme, force=force)
    write_page(folder / 'index.html', page('BBS時光庫', index, INDEX_KEYS), force=force)
    if stats is not None:
        stats['written'] = written
    return count
