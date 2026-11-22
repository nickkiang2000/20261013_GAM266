"""Board heading and ordered sticky snapshots, separate from article numbering."""
from datetime import datetime, timezone, timedelta
from hashlib import sha256
from html import escape
import json
import re
from article_metadata import list_cells


def heading(screen, board):
    first = screen.split('\n', 1)[0]
    label = re.search(r'看板\s*[《〈\[【]\s*'+re.escape(board)+r'\s*[》〉\]】]', first, re.I)
    owner = re.search(r'[【\[]板主.*?[】\]]', first)
    if not label or not owner or owner.end() > label.start():
        raise ValueError('Board heading cannot be verified')
    return first[owner.end():label.start()].strip()


def sticky_line(row, cursor='>'):
    value = row.lstrip()
    if value.startswith(cursor): value = value[len(cursor):].lstrip()
    return bool(re.match(r'^[★☆*]\s', value))


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS board_metadata(site TEXT,board TEXT,data TEXT,PRIMARY KEY(site,board))')


def save(db, site, board, snapshot):
    if snapshot.get('board') != board or snapshot.get('complete') is not True:
        raise ValueError('Incomplete board snapshot')
    aids = [row['aid'] for row in snapshot.get('pins', [])]
    if len(set(aids)) != len(aids) or any(not re.fullmatch(r'[A-Za-z0-9_-]{8}', aid) for aid in aids):
        raise ValueError('Invalid sticky identity')
    with db:
        schema(db)
        db.execute('INSERT INTO board_metadata VALUES(?,?,?) ON CONFLICT(site,board) DO UPDATE SET data=excluded.data',
                   (site, board, json.dumps(dict(snapshot, captured_at=datetime.now(timezone(timedelta(hours=8))).isoformat()),ensure_ascii=False)))


def load(db, site, board):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='board_metadata'").fetchone():return {}
    row=db.execute('SELECT data FROM board_metadata WHERE site=? AND board=?',(site,board)).fetchone()
    return json.loads(row[0]) if row else {}


def render(db, site, board):
    data=load(db,site,board)
    if not data:return ''
    title='<p class="board-title">'+escape(data.get('title',''))+'</p>'
    items=[]
    for pin in data.get('pins',[]):
        saved=db.execute('SELECT 1 FROM articles WHERE site=? AND board=? AND aid=?',(site,board,pin['aid'])).fetchone()
        label='★ '+pin.get('title',pin['aid'])
        if saved:
            key=sha256(json.dumps((site,board,pin['aid'])).encode()).hexdigest()
            label='<a href="articles/'+key+'.html">'+escape(label)+'</a>'
        else:label=escape(label)+'（正文尚未備份）'
        items.append('<li>'+label+'</li>')
    return title+'<aside class="sticky-posts"><strong>置底文章</strong><ul>'+''.join(items)+'</ul><small>備份時狀態：'+escape(data.get('captured_at',''))+'</small></aside>'


def render_rows(db, site, board):
    data = load(db, site, board)
    items = []
    for pin in data.get('pins', []):
        key = sha256(json.dumps((site, board, pin['aid'])).encode()).hexdigest()
        saved = db.execute('SELECT body,data FROM articles WHERE site=? AND board=? AND aid=?',
                           (site, board, pin['aid'])).fetchone()
        post = json.loads(saved[1]) if saved and saved[1] else {}
        post = dict(pin, **post)
        if 'push_number' in pin: post['push_number'] = pin['push_number']
        label = list_cells(None, post, pin.get('title', pin['aid']), saved[0] if saved else '',
                           site=site, board=board, aid=pin['aid'], sticky=True)
        if saved:
            label = '<a id="pin-' + key + '" href="articles/' + key + '.html#sticky">' + label + '</a>'
        else:
            label = '<div class="sticky-missing">' + label + '<small>（正文尚未備份）</small></div>'
        items.append('<li class="sticky-entry">' + label + '</li>')
    return ''.join(items)
