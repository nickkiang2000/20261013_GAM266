"""Persistent current article exceptions; detailed run history stays in log/."""
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from hashlib import sha256
from html import escape
import json
from pathlib import Path


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


def resolve_index_query(db, site, board, index):
    db.execute('''DELETE FROM article_issues WHERE site=? AND board=? AND issue_key=?''',
               (site, board, f'index:{index}'))


def log_evidence(db, directory):
    saved = {(site, board, aid): idx for site, board, aid, idx in db.execute(
        'SELECT site,board,aid,source_index FROM articles')}
    occupied = {(site, board, idx) for (site, board, _), idx in saved.items()}
    occupied.update(db.execute('SELECT site,board,idx FROM unavailable_articles'))
    verified = {}
    ambiguous = {}
    for path in sorted(directory.glob('*.log')):
        board_key = None
        seen = {}
        try:
            with path.open(encoding='utf-8', errors='replace') as log:
                for line in log:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get('event') == 'start':
                        board_key = event.get('site'), event.get('board')
                    if (not board_key or event.get('event') != 'article' or event.get('outcome')
                            not in {'saved', 'updated', 'unchanged', 'reused_local', 'verified_earlier_in_batch'}):
                        continue
                    idx, aid = event.get('index'), event.get('aid')
                    if type(idx) is not int or event.get('returned_index') != idx or not aid:
                        continue
                    stored_index = saved.get((*board_key, aid))
                    if stored_index is None:
                        continue
                    if idx == stored_index:
                        try:
                            timestamp = datetime.fromisoformat(event['time']).timestamp()
                        except (KeyError, TypeError, ValueError):
                            continue
                        identity = (*board_key, idx, aid)
                        verified[identity] = max(timestamp, verified.get(identity, 0))
                    seen.setdefault(aid, set()).add(idx)
        except OSError:
            continue
        if not board_key:
            continue
        for aid, indexes in seen.items():
            stored_index = saved[*board_key, aid]
            if len(indexes) > 1 and stored_index in indexes:
                for idx in indexes - {stored_index}:
                    if (*board_key, idx) not in occupied:
                        ambiguous.setdefault((*board_key, idx), set()).add((aid, stored_index))
    return verified, ambiguous


def matching_test(db, directory, site, board, aid, last_seen):
    original = db.execute('SELECT body,data FROM articles WHERE site=? AND board=? AND aid=?',
                          (site, board, aid)).fetchone()
    if not original:
        return False
    for path in (directory / f'{site}_{board}' / aid).glob('*/*/articles.sqlite3'):
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as sample:
                row = sample.execute('''SELECT body,data FROM articles
                    WHERE site=? AND board=? AND aid=? AND fetched>?''', (site, board, aid, last_seen)).fetchone()
            if row and row[0] == original[0]:
                test = json.loads(row[1])
                coverage = test.get('ansi_coverage', {})
                if (coverage.get('total', 0) > 0 and coverage.get('matched') == coverage['total']
                        and test.get('ansi_lines') == json.loads(original[1]).get('ansi_lines')):
                    return True
        except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
            continue
    return False


def report(db, evidence=None):
    verified, ambiguous = evidence if evidence else ({}, {})
    samples = Path(__file__).resolve().parent / 'test-downloads'
    colors = {(site, board, aid) for site, board, aid in db.execute(
        'SELECT site,board,aid FROM color_line_checks')}
    read_details = {}
    entries = []
    for site, board, idx, aid, reason, first, last, attempts, key in db.execute('''
            SELECT site,board,idx,aid,reason,first_seen,last_seen,attempts,issue_key
            FROM article_issues ORDER BY site,board,idx,aid'''):
        if aid:
            saved = db.execute('SELECT aid FROM articles WHERE site=? AND board=? AND aid=?',
                               (site, board, aid)).fetchone()
        else:
            candidates = db.execute('SELECT aid FROM articles WHERE site=? AND board=? AND source_index=? LIMIT 2',
                                    (site, board, idx)).fetchall()
            saved = candidates[0] if len(candidates) == 1 else None
            if saved and verified.get((site, board, idx, saved[0]), 0) > int(last):
                continue
        if saved and aid and not key.startswith('html:'):
            if matching_test(db, samples, site, board, aid, last):
                continue
            if (site, board, aid) in colors:
                read_details[(site, board, aid)] = reason
                continue
        entries.append((site, board, idx, aid,
                        'HTML 呈現失敗' if key.startswith('html:') else
                        ('讀取待查，保留已存文章' if saved else '未能下載／確認全文'),
                        reason, saved[0] if saved else None,
                        f'{attempts} 次；最近 {datetime.fromtimestamp(last):%Y-%m-%d %H:%M:%S}' +
                        ('；連結依本地編號提供，原紀錄未取得 AID' if saved and not aid else '')))
    for site, board, idx, aid, matched, total in db.execute(
            'SELECT site,board,idx,aid,matched,total FROM color_line_checks ORDER BY site,board,idx,aid'):
        saved = db.execute('SELECT 1 FROM articles WHERE site=? AND board=? AND aid=?',
                           (site, board, aid)).fetchone()
        reason = f'配色對齊 {matched}／{total} 行；不代表正文缺少相同行數。'
        if (site, board, aid) in read_details:
            reason += ' 讀取紀錄：' + read_details[(site, board, aid)]
        entries.append((site, board, idx, aid, '待核色', reason,
                        aid if saved else None, '可先查看已存內容，再決定是否特案處理'))
    for site, board, idx, aid, status in db.execute(
            'SELECT site,board,idx,aid,status FROM unavailable_articles ORDER BY site,board,idx'):
        saved = db.execute('SELECT 1 FROM articles WHERE site=? AND board=? AND aid=?',
                           (site, board, aid)).fetchone()
        label = {'deleted': '被刪除', 'locked': '已鎖定', 'empty': '站上無正文'}[status]
        reason = '站方顯示此文章無內容。' if status == 'empty' else '站方已確認不可讀，已跳過。'
        entries.append((site, board, idx, aid, label, reason, aid if saved else None,
                        '保留舊備份' if saved else '索引顯示站方狀態'))
    for (site, board, idx), matches in ambiguous.items():
        if any(entry[:3] == (site, board, idx) for entry in entries):
            continue
        for aid, stored_index in sorted(matches):
            entries.append((site, board, idx, aid, '編號對應待查',
                            f'同批 log 將 {idx}、{stored_index} 對應至同一 AID。',
                            aid, f'該 AID 目前存為 {stored_index}；請核對站上列表'))
    entries.sort(key=lambda entry: (entry[0], entry[1].casefold(), entry[1],
                                    entry[2] is None, entry[2] or 0, entry[3] or ''))
    header = ('<section id="issue-records"><h2>錯漏文章</h2>'
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
            key = sha256(json.dumps((site, board, saved)).encode()).hexdigest()
            link = f'<a href="articles/{key}.html">查看已存文章</a>'
        parts.append('<tr>' + ''.join(f'<td>{escape(str(value))}</td>' for value in values) + f'<td>{link}</td></tr>')
    return ''.join(parts) + '</tbody></table></div></section>'
