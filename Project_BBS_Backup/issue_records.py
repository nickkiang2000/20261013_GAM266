"""Persistent current article exceptions; detailed run history stays in log/."""
import re
import time
from datetime import datetime
from hashlib import sha256
from html import escape
import json


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS article_issues(
        site TEXT, board TEXT, issue_key TEXT, idx INTEGER, aid TEXT, reason TEXT,
        first_seen REAL, last_seen REAL, attempts INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(site,board,issue_key))''')
    db.execute('CREATE TABLE IF NOT EXISTS archive_migrations(name TEXT PRIMARY KEY)')
    if db.execute("SELECT 1 FROM archive_migrations WHERE name='article_issues_v1'").fetchone():
        return
    # Old failures include recovered errors and station-wide failures. Only import
    # article-specific failures with no current article or confirmed station status.
    for site, board, idx, reason, created in db.execute(
            'SELECT site,board,idx,reason,created FROM failures ORDER BY created').fetchall():
        match = re.match(r'^#?([A-Za-z0-9_-]{8})\s+(.+)', reason or '')
        aid = match[1] if match else None
        if idx is None and aid is None:
            continue
        if db.execute('''SELECT 1 FROM articles WHERE site=? AND board=?
                         AND (aid=? OR source_index=?)''', (site, board, aid, idx)).fetchone():
            continue
        if db.execute('''SELECT 1 FROM unavailable_articles WHERE site=? AND board=?
                         AND (aid=? OR idx=?)''', (site, board, aid, idx)).fetchone():
            continue
        if idx is None and aid:
            previous = db.execute('''SELECT idx FROM article_issues
                WHERE site=? AND board=? AND aid=? AND idx IS NOT NULL''', (site, board, aid)).fetchone()
            idx = previous[0] if previous else None
        record(db, site, board, idx, aid, match[2] if match else reason, now=created)
    db.execute("INSERT INTO archive_migrations VALUES('article_issues_v1')")


def record(db, site, board, index, aid, reason, *, now=None, kind='read'):
    if index is None and not aid:
        return
    if index is None and aid:
        previous_index = db.execute('''SELECT source_index FROM articles
            WHERE site=? AND board=? AND aid=?''', (site, board, aid)).fetchone()
        if previous_index:
            index = previous_index[0]
    now = time.time() if now is None else now
    key = ('html:' if kind == 'html' else 'aid:') + aid if aid else f'index:{index}'
    # Merge an earlier query failure once its AID becomes known.
    previous = db.execute('''SELECT first_seen,attempts FROM article_issues
        WHERE site=? AND board=? AND issue_key=?''', (site, board, f'index:{index}')).fetchone() if aid and kind == 'read' else None
    if previous:
        db.execute('DELETE FROM article_issues WHERE site=? AND board=? AND issue_key=?',
                   (site, board, f'index:{index}'))
    db.execute('''INSERT INTO article_issues VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(site,board,issue_key) DO UPDATE SET
        idx=coalesce(excluded.idx,idx), reason=excluded.reason,
        first_seen=min(first_seen,excluded.first_seen), last_seen=excluded.last_seen,
        attempts=attempts+excluded.attempts''',
        (site, board, key, index, aid, reason, previous[0] if previous else now,
         now, 1 + (previous[1] if previous else 0)))


def resolve(db, site, board, index, aid):
    # A known AID is the identity: do not clear another article's error if board
    # numbering changed. An index-only query failure can be resolved by that index.
    db.execute('''DELETE FROM article_issues WHERE site=? AND board=?
        AND issue_key NOT LIKE 'html:%' AND (aid=? OR (aid IS NULL AND idx=?))''', (site, board, aid, index))


def report(db):
    entries = []
    for site, board, idx, aid, reason, first, last, attempts, key in db.execute('''
            SELECT site,board,idx,aid,reason,first_seen,last_seen,attempts,issue_key
            FROM article_issues ORDER BY site,board,idx,aid'''):
        saved = db.execute('SELECT 1 FROM articles WHERE site=? AND board=? AND aid=?',
                           (site, board, aid)).fetchone()
        entries.append((site, board, idx, aid,
                        'HTML 呈現失敗' if key.startswith('html:') else
                        ('本次讀取失敗，保留舊備份' if saved else '未能下載／確認全文'),
                        reason, bool(saved), f'{attempts} 次；最近 {datetime.fromtimestamp(last):%Y-%m-%d %H:%M:%S}'))
    for site, board, idx, aid, matched, total in db.execute(
            'SELECT site,board,idx,aid,matched,total FROM color_line_checks ORDER BY site,board,idx,aid'):
        saved = db.execute('SELECT 1 FROM articles WHERE site=? AND board=? AND aid=?',
                           (site, board, aid)).fetchone()
        entries.append((site, board, idx, aid, '待核色', f'配色對齊 {matched}／{total} 行；不代表正文缺少相同行數。',
                        bool(saved), '可先查看已存內容，再決定是否特案處理'))
    for site, board, idx, aid, status in db.execute(
            'SELECT site,board,idx,aid,status FROM unavailable_articles ORDER BY site,board,idx'):
        saved = db.execute('SELECT 1 FROM articles WHERE site=? AND board=? AND aid=?',
                           (site, board, aid)).fetchone()
        entries.append((site, board, idx, aid, '被刪除' if status == 'deleted' else '已鎖定',
                        '站方已確認不可讀，已跳過。', bool(saved), '保留舊備份' if saved else '索引顯示站方狀態'))
    header = ('<section id="issue-records"><h2>錯漏紀錄</h2>'
              '<p>此表反映本機已遇到的問題，不是全板完整性證明；未掃描文章不在此表。'
              '編號可能隨站上文章異動，特案查找請優先使用 AID。補抓成功會清除對應待查，'
              '核色成功會清除待核色；歷次診斷仍保留在程式 log 資料夾。</p>')
    if not entries:
        return header + '<p>目前沒有已記錄的錯漏項目。</p></section>'
    parts = [header, f'<p>共 {len(entries)} 筆紀錄；同篇可能同時有讀取及配色問題。</p>',
             '<div class="records-scroll"><table><thead><tr>' +
             ''.join(f'<th>{name}</th>' for name in ('站台／看板', '編號', 'AID', '狀態', '原因', '處理資訊', '內容')) +
             '</tr></thead><tbody>']
    for site, board, idx, aid, status, reason, saved, detail in entries:
        values = (f'{site} / {board}', str(idx) if idx is not None else '未取得', aid or '未取得', status, reason, detail)
        link = '尚無可讀存檔'
        if saved:
            key = sha256(json.dumps((site, board, aid)).encode()).hexdigest()
            link = f'<a href="articles/{key}.html">查看已存文章</a>'
        parts.append('<tr>' + ''.join(f'<td>{escape(str(value))}</td>' for value in values) + f'<td>{link}</td></tr>')
    return ''.join(parts) + '</tbody></table></div></section>'
