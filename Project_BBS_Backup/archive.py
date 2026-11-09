"""Portable offline archive pages. All links work directly from disk."""

from hashlib import sha256
from html import escape
from itertools import groupby
import json


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
nav { display: flex; flex-wrap: wrap; gap: 1.25rem; }
.pagination { align-items: center; gap: .75rem; }
.pagination button { font: inherit; padding: .25rem .65rem; }
.meta { color: #b2bfd0; font-size: .88rem; }
'''

PAGE_SIZE = 20

INDEX_KEYS = '''
const boards = Array.from(document.querySelectorAll('.board'), section => ({
    pages: Array.from(section.querySelectorAll('.entries')),
    entries: Array.from(section.querySelectorAll('.entries a')),
    older: section.querySelector('.older'),
    newer: section.querySelector('.newer'),
    label: section.querySelector('.page-label'),
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
    board.label.textContent = `第 ${page + 1}／${board.pages.length} 頁`;
}

function select(board, index, focus = true) {
    if (activeBoard?.position >= 0) {
        activeBoard.entries[activeBoard.position].removeAttribute('aria-current');
    }
    activeBoard = board;
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
    } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        board.entries[board.position].click();
    }
});
'''

ARTICLE_KEYS = '''
document.addEventListener('keydown', event => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
        event.preventDefault();
        const viewport = document.scrollingElement;
        const remaining = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop;
        const pageForward = event.key === 'ArrowRight';
        if (remaining > 1) {
            const distance = pageForward
                ? viewport.clientHeight
                : parseFloat(getComputedStyle(document.querySelector('pre')).lineHeight);
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


def page(title, body, script):
    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>{STYLE}</style></head>
<body>{body}<script>{script}</script></body></html>'''


def write_page(path, content):
    temp = path.with_suffix('.html.tmp')
    temp.write_text(content, encoding='utf-8')
    temp.replace(path)


def export_html(db, folder):
    articles = folder / 'articles'
    articles.mkdir(exist_ok=True)
    rows = db.execute('''SELECT site, board, aid, title, body, data, source_index FROM articles
                        ORDER BY site, board COLLATE NOCASE, board,
                        source_index IS NOT NULL, source_index ASC, fetched ASC, aid''')
    count = 0
    sections = []
    for (site, board), group in groupby(rows, key=lambda row: row[:2]):
        items = list(group)
        keys = [sha256(json.dumps(row[:3]).encode()).hexdigest() for row in items]
        entries = []
        for i, (_, _, aid, title, body, data, source_index) in enumerate(items):
            key = keys[i]
            title = title or '(無標題)'
            label = f'{source_index} · ' if source_index is not None else ''
            author = json.loads(data).get('author') or ''
            label += f'{author} · ' if author else ''
            entries.append(f'<li><a id="a-{key}" href="articles/{key}.html">'
                           f'<span class="meta">{escape(label)}</span>{escape(title)}</a></li>')
            links = [f'<a id="back" href="../index.html#a-{key}">返回列表</a>']
            if i > 0:
                links.append(f'<a id="previous" href="{keys[i-1]}.html">上一篇 ↑</a>')
            if i + 1 < len(keys):
                links.append(f'<a id="next" href="{keys[i+1]}.html">下一篇 ↓</a>')
            nav = '<nav>' + ''.join(links) + '</nav>'
            article = (f'{nav}<h1>{escape(title)}</h1><p class="meta">'
                       f'{escape(site)} / {escape(board)} · {escape(aid)}</p>'
                       f'<pre>{escape(body)}</pre>')
            write_page(articles / f'{key}.html', page(title, article, ARTICLE_KEYS))
        count += len(items)
        # Anchor pages at the newest end so the first view shows the latest 20.
        first_size = len(entries) % PAGE_SIZE or PAGE_SIZE
        batches = [entries[:first_size]] + [entries[start:start + PAGE_SIZE]
                                            for start in range(first_size, len(entries), PAGE_SIZE)]
        pages = []
        for i, batch in enumerate(batches):
            hidden = ' hidden' if i < len(batches) - 1 else ''
            pages.append(f'<ul class="entries"{hidden}>' + ''.join(batch) + '</ul>')
        pagination = ('<nav class="pagination"><button class="older" type="button">← 較舊 20 篇</button>'
                      f'<span class="page-label" aria-live="polite">第 {len(batches)}／{len(batches)} 頁</span>'
                      '<button class="newer" type="button" disabled>較新 20 篇 →</button></nav>')
        sections.append(f'<section class="board"><h2>{escape(site)} / {escape(board)} ({len(items)})</h2>'
                        + pagination + ''.join(pages) + '</section>')
    index = (f'<h1>BBS 離線文章</h1><p>共 {count} 篇。↑／↓ 選取，Enter／→ 開啟，'
             'Page Up／Page Down 切換較舊／較新的文章。'
             '閱讀時按 ← 或 Esc 返回。內文以等寬純文字顯示。</p>' + ''.join(sections))
    write_page(folder / 'index.html', page('BBS 離線文章', index, INDEX_KEYS))
    return count
