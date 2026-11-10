"""Local PTT and PTT2 downloader."""
import json
import logging
import secrets
import sqlite3
import threading
import time
import re
import webbrowser
from contextlib import closing
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from archive import export_html
from local_state import JobLog, load_preferences, save_preferences

ROOT = Path(__file__).resolve().parent
APP_VERSION = 'v2.3.0'
TOKEN = secrets.token_urlsafe(32)
STATE = {'running': False, 'message': '尚未登入／實站驗證', 'saved': 0, 'skipped': 0}
LOCK = threading.Lock()
STOP = threading.Event()
READERS = {}
logging.disable(logging.CRITICAL)


ARTICLE_TABLE = '''CREATE TABLE IF NOT EXISTS articles(
    site TEXT NOT NULL, board TEXT, aid TEXT, title TEXT, body TEXT, data TEXT,
    fetched REAL, source_index INTEGER, PRIMARY KEY(site,board,aid))'''
SITES = {'PTT': 'PTT1', 'PTT2': 'PTT2'}
DIRECTIONS = {'newest': '新到舊', 'oldest': '舊到新'}
BEFORE_READ_SECONDS = 2
BETWEEN_ITEMS_SECONDS = 3
# Increment when the stored capture format changes. HTML-only changes can be re-exported.
CAPTURE_VERSION = 1


class ArticleReadError(ValueError):
    """An individual article could not be saved safely."""


def database(folder):
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / 'articles.sqlite3')
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in db.execute('PRAGMA table_info(articles)')}
            if columns and 'site' not in columns:
                db.execute('ALTER TABLE articles RENAME TO articles_v1')
                db.execute(ARTICLE_TABLE)
                db.execute('''INSERT INTO articles (site,board,aid,title,body,data,fetched)
                              SELECT 'PTT2',board,aid,title,body,data,fetched FROM articles_v1''')
                db.execute('DROP TABLE articles_v1')
            else:
                db.execute(ARTICLE_TABLE)
            db.execute('''CREATE TABLE IF NOT EXISTS failures(
                          board TEXT, idx INTEGER, reason TEXT, created REAL,
                          site TEXT NOT NULL DEFAULT 'PTT2')''')
            if 'site' not in {row[1] for row in db.execute('PRAGMA table_info(failures)')}:
                db.execute("ALTER TABLE failures ADD COLUMN site TEXT NOT NULL DEFAULT 'PTT2'")
            db.execute('''CREATE TABLE IF NOT EXISTS batch_ranges(
                          site TEXT, board TEXT, first_index INTEGER, last_index INTEGER,
                          completed INTEGER NOT NULL DEFAULT 0, direction TEXT NOT NULL DEFAULT 'newest',
                          PRIMARY KEY(site,board))''')
            batch_columns = {row[1] for row in db.execute('PRAGMA table_info(batch_ranges)')}
            if 'completed' not in batch_columns:
                db.execute('ALTER TABLE batch_ranges ADD COLUMN completed INTEGER NOT NULL DEFAULT 0')
            if 'direction' not in batch_columns:
                db.execute("ALTER TABLE batch_ranges ADD COLUMN direction TEXT NOT NULL DEFAULT 'newest'")
            db.execute('''CREATE TABLE IF NOT EXISTS batch_verified(
                          site TEXT, board TEXT, aid TEXT, PRIMARY KEY(site,board,aid))''')
            db.execute('''CREATE TABLE IF NOT EXISTS unavailable_articles(
                          site TEXT, board TEXT, idx INTEGER, aid TEXT, status TEXT,
                          PRIMARY KEY(site,board,idx))''')
            db.execute('''CREATE TABLE IF NOT EXISTS color_line_checks(
                          site TEXT, board TEXT, aid TEXT, idx INTEGER, matched INTEGER, total INTEGER,
                          PRIMARY KEY(site,board,aid))''')
    except Exception:
        db.close()
        raise
    return db


def save_article(db, board, post, *, site='PTT2', source_index=None, commit=True):
    aid = post.get('aid')
    body = post.get('full_content')
    if not aid or not body or not (post.get('pass_format_check') or post.get('headerless')
                                   or post.get('terminal_verified')):
        raise ValueError('文章缺少 AID、全文或格式檢查未通過')
    db.execute('''INSERT INTO articles VALUES(?,?,?,?,?,?,?,?)
                  ON CONFLICT(site, board, aid) DO UPDATE SET
                  title=excluded.title, body=excluded.body, data=excluded.data,
                  fetched=excluded.fetched, source_index=coalesce(excluded.source_index, articles.source_index)''',
               (site, board, aid, post.get('title',''), body,
                json.dumps(post,ensure_ascii=False), time.time(), source_index))
    if commit:
        db.commit()


def record_failure(db, site, board, index, reason):
    db.execute('INSERT INTO failures (site,board,idx,reason,created) VALUES(?,?,?,?,?)',
               (site, board, index, reason, time.time()))
    db.commit()


def show_issue(index, aid, message):
    issues = STATE.setdefault('issues', [])
    issues.append({'index': index, 'aid': aid, 'message': message})


def find_pending_range(folder, site, board, *, with_progress=False):
    db_path = folder / 'articles.sqlite3'
    if not db_path.is_file():
        return None
    with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(batch_ranges)')}
        if not columns:
            return None
        condition = ' AND completed=0' if 'completed' in columns else ''
        direction = 'direction' if 'direction' in columns else "'newest'"
        pending = db.execute(f'SELECT first_index,last_index,{direction} FROM batch_ranges '
                             'WHERE site=? AND board=?' + condition, (site, board)).fetchone()
        if not pending or not with_progress:
            return pending
        first, last, direction = pending
        has_checkpoints = db.execute("SELECT 1 FROM sqlite_master WHERE name='batch_verified'").fetchone()
        reusable = db.execute('SELECT count(*) FROM batch_verified WHERE site=? AND board=?',
                              (site, board)).fetchone()[0] if has_checkpoints else 0
        return {'first': first, 'last': last, 'direction': direction, 'reusable': reusable}


def stored_article(db, site, board, aid):
    row = db.execute('SELECT data FROM articles WHERE site=? AND board=? AND aid=?',
                     (site, board, aid)).fetchone()
    return json.loads(row[0]) if row else None


def batch_bounds(newest, requested_start, count, direction):
    """Return the inclusive low and high bounds for a new batch."""
    first = requested_start or newest
    if not 1 <= first <= newest:
        return None
    if direction == 'newest':
        return max(1, first - count + 1), first
    return first, min(newest, first + count - 1)


def confirmed_deleted(api, query, index):
    # The SDK can report DELETED on a timeout with a stale listing. Require the
    # actual selected row, index and explicit BBS deletion marker.
    if query.get('aid') or not str(query.get('post_status', '')).upper().startswith('DELETED_BY_'):
        return False
    queue = api.connect_core.get_screen_queue()
    if not queue:
        return False
    pattern = rf'\s*0*{index}\s+.*?\d{{1,2}}/\d{{1,2}}\s+-\s+□\s*[（(]本文已被(?:刪除|吃掉)[）)]'
    return any(row.startswith(api.cursor) and re.match(pattern, row[len(api.cursor):])
               for row in queue[-1].splitlines())


def note_unavailable(db, site, board, index, aid, status):
    with db:
        if index is not None:
            db.execute('INSERT OR REPLACE INTO unavailable_articles VALUES(?,?,?,?,?)',
                       (site, board, index, aid, status))
    STATE['unavailable'] = STATE.get('unavailable', 0) + 1
    show_issue(index, aid, '（被刪除）' if status == 'deleted' else '（已鎖定）')


def publish_color_checks(db, folder):
    path = folder / 'color_line_check.txt'
    rows = db.execute('''SELECT site,board,idx,aid,matched,total FROM color_line_checks
                         ORDER BY site,board,idx,aid''').fetchall()
    if not rows and not path.exists():
        return
    lines = ['站台\t看板\t編號\tAID\t配色對齊行數']
    lines.extend(f'{site}\t{board}\t{idx if idx is not None else "編號未取得"}\t{aid}\t{matched}/{total}'
                 for site, board, idx, aid, matched, total in rows)
    temp = path.with_suffix('.txt.tmp')
    temp.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
    temp.replace(path)


def archive_destination(root, site, board):
    name = f'{site}_{board}'
    if root.name.casefold() == name.casefold():
        return root
    destination = root / name
    if (destination / 'articles.sqlite3').is_file():
        return destination
    legacy = root / 'articles.sqlite3'
    if legacy.is_file():
        # Keep a matching older archive in place so its pending batch remains usable.
        with closing(sqlite3.connect(legacy.as_uri() + '?mode=ro', uri=True)) as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(articles)')}
            if 'site' in columns:
                match = db.execute('SELECT 1 FROM articles WHERE site=? AND board=? COLLATE NOCASE LIMIT 1',
                                   (site, board)).fetchone()
            elif columns and site == 'PTT2':
                match = db.execute('SELECT 1 FROM articles WHERE board=? COLLATE NOCASE LIMIT 1', (board,)).fetchone()
            else:
                match = None
            has_batches = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='batch_ranges'").fetchone()
            if match or (has_batches and db.execute(
                    'SELECT 1 FROM batch_ranges WHERE site=? AND board=? COLLATE NOCASE LIMIT 1',
                    (site, board)).fetchone()):
                return root
    return destination


def export_progress(done, total):
    STATE.update(exported=done, export_total=total)


def publish_archive(db, folder):
    STATE['phase'] = 'export'
    publish_color_checks(db, folder)
    count = export_html(db, folder, progress=export_progress)
    STATE['index_path'] = str(folder / 'index.html')
    reader = secrets.token_urlsafe(24)
    READERS[reader] = folder.resolve()
    STATE['index_url'] = f'/reader/{reader}/index.html'
    return count


def export_existing(folder):
    job = None
    try:
        STATE['folder'] = str(folder)
        if not (folder / 'articles.sqlite3').is_file():
            STATE['message'] = '找不到 articles.sqlite3，請選擇先前的備份資料夾。'
            return
        job = JobLog()
        STATE['log_path'] = str(job.path)
        job.event('start', version=APP_VERSION, operation='refresh_offline')
        with closing(database(folder)) as db:
            STATE['local_count'] = db.execute('SELECT count(*) FROM articles').fetchone()[0]
            count = publish_archive(db, folder)
        STATE['message'] = '離線內容重整完成。'
        job.event('finish', exported=count)
    except Exception as exc:
        STATE['message'] = f'離線內容重整失敗：{type(exc).__name__}'
        if job:
            job.error('refresh_offline', exc)
    finally:
        STATE.update(running=False, phase='done')


def complete_capture(post):
    coverage = post.get('ansi_coverage') or {}
    return (post.get('capture_version') == CAPTURE_VERSION and coverage.get('total', 0) > 0
            and coverage.get('matched') == coverage['total'])


def download_article(api, db, site, board, aid, *, source_index=None, existing=None, checkpoint=False):
    from ansi_capture import read_colored_post

    post = read_colored_post(api, board, aid)
    STATE['ansi_diagnostics'] = post.get('ansi_diagnostics', {})
    if existing and existing.get('full_content'):
        previous_rows = existing['full_content'].split('\n')
        STATE['ansi_diagnostics'].update(previous_lines=len(previous_rows),
                                         previous_blank_lines=sum(not row.strip() for row in previous_rows))
    if post.get('aid') != aid:
        raise ArticleReadError('文章代碼和要求的 AID 不一致')
    if source_index is None and type(post.get('index')) is int and post['index'] > 0:
        source_index = post['index']
    if post.get('is_lock'):
        note_unavailable(db, site, board, source_index, aid, 'locked')
        return 'locked', False
    if not post.get('full_content'):
        raise ArticleReadError('終端未回傳可保存的正文，未建立本次文章 HTML')
    if not post.get('pass_format_check'):
        body = post['full_content']
        header = ('作者  ', '標題  ', '時間  ')
        has_metadata = any(line.lstrip().startswith(header) for line in body.split('\n', 3)[:3])
        reached_end = post.get('ansi_diagnostics', {}).get('reached_end')
        if not reached_end or not body.strip():
            raise ArticleReadError('正文格式未通過，且無法確認已讀到文末')
        post['terminal_verified'] = True
        if not has_metadata:
            post['headerless'] = True
    post['capture_version'] = CAPTURE_VERSION
    verified = complete_capture(post)
    STATE['ansi_samples'] = STATE.get('ansi_samples', 0) + 1
    STATE['ansi_colored'] = STATE.get('ansi_colored', 0) + bool(post.get('ansi_lines'))
    STATE['ansi_coverage'] = post.get('ansi_coverage', {})
    fields = ('title', 'author', 'date', 'full_content', 'ansi_lines', 'comments')
    unchanged = verified and existing and complete_capture(existing) and all(
        json.dumps(post.get(key), sort_keys=True) == json.dumps(existing.get(key), sort_keys=True)
        for key in fields)
    keep_previous = (not verified and existing and complete_capture(existing)
                     and post['full_content'] == existing.get('full_content'))
    coverage = STATE['ansi_coverage']
    matched = coverage.get('matched', 0)
    total = coverage.get('total') or post['full_content'].count('\n') + 1
    with db:
        if unchanged or keep_previous:
            db.execute('UPDATE articles SET source_index=coalesce(?,source_index) WHERE site=? AND board=? AND aid=?',
                       (source_index, site, board, aid))
        else:
            save_article(db, board, post, site=site, source_index=source_index, commit=False)
        db.execute('DELETE FROM unavailable_articles WHERE site=? AND board=? AND (idx=? OR aid=?)',
                   (site, board, source_index, aid))
        if verified:
            db.execute('DELETE FROM color_line_checks WHERE site=? AND board=? AND aid=?', (site, board, aid))
        else:
            db.execute('''INSERT INTO color_line_checks VALUES(?,?,?,?,?,?)
                          ON CONFLICT(site,board,aid) DO UPDATE SET idx=coalesce(excluded.idx,idx),
                          matched=excluded.matched,total=excluded.total''',
                       (site, board, aid, source_index, matched, total))
        if checkpoint:
            db.execute('INSERT OR IGNORE INTO batch_verified VALUES(?,?,?)', (site, board, aid))
    if unchanged:
        STATE['skipped'] += 1
        return 'unchanged', verified
    if keep_previous:
        STATE['kept_previous'] = STATE.get('kept_previous', 0) + 1
        show_issue(source_index, aid, f'保留舊備份，本次配色對齊 {matched}／{total} 行')
        return 'kept_previous_color', verified
    STATE['saved'] += 1
    STATE['updated'] = STATE['saved']
    if not verified:
        STATE['ansi_review'] = STATE.get('ansi_review', 0) + 1
        show_issue(source_index, aid, f'已存正文，配色對齊 {matched}／{total} 行')
    return 'saved', verified


def work(config):
    api = None
    db = None
    idx = None
    aid = None
    job = None
    stage = 'prepare'
    stage_started = started = time.monotonic()
    article_started = None
    query_seconds = None
    result = 'done'
    board = config['board']
    site = config['site']
    fresh_seconds = 0.0
    try:
        import PyPtt

        folder = Path(config['folder']).expanduser().resolve()
        STATE['folder'] = str(folder)
        db = database(folder)
        STATE['local_count'] = db.execute('SELECT count(*) FROM articles').fetchone()[0]
        job = JobLog()
        STATE['log_path'] = str(job.path)
        mode = 'single' if config.get('aid') else config.get('mode', 'new')
        job.event('start', version=APP_VERSION, site=site, board=board, capture_version=CAPTURE_VERSION,
                  mode=mode, requested_aid=config.get('aid') or None,
                  requested_start=config.get('start') if mode == 'new' else None,
                  requested_count=config.get('count') if mode == 'new' else None,
                  requested_direction=config.get('direction', 'newest') if mode == 'new' else None,
                  local_count=STATE['local_count'], before_read_seconds=BEFORE_READ_SECONDS,
                  between_items_seconds=BETWEEN_ITEMS_SECONDS)
        stage = 'login'
        stage_started = time.monotonic()
        STATE['message'] = f'正在登入 {site}…'
        api = PyPtt.API(host=getattr(PyPtt.HOST, SITES[site]), log_level=PyPtt.LogLevel.SILENT,
                        check_update=False, verify_ssl=True)
        api.login(config.pop('user'), config.pop('password'), kick_other_session=False)
        job.event('login', seconds=round(time.monotonic() - stage_started, 3))
        if STOP.is_set():
            result = 'stopped'
            STATE['message'] = '已中止。'
            return
        if config.get('aid'):
            STATE.update(phase='download', scanned=0, batch_total=1)
            aid = config['aid']
            stage = 'read_article'
            article_started = stage_started = time.monotonic()
            STATE['message'] = f'正在讀取 {board} #{aid}…'
            try:
                outcome, _ = download_article(api, db, site, board, aid,
                                              existing=stored_article(db, site, board, aid))
            except ArticleReadError as exc:
                result = 'pending'
                STATE.update(scanned=1, unresolved=1)
                show_issue(None, aid, str(exc))
                job.event('article', aid=aid, outcome='unreadable', reason=str(exc),
                          capture=STATE['ansi_diagnostics'], seconds=round(time.monotonic() - article_started, 3))
                STATE['message'] = '指定文章待查。'
                record_failure(db, site, board, None, f'{aid} {exc}')
                return
            job.event('article', aid=aid, outcome=outcome, coverage=STATE.get('ansi_coverage', {}),
                      capture=STATE['ansi_diagnostics'], seconds=round(time.monotonic() - article_started, 3))
            STATE['scanned'] = 1
            STATE['message'] = '指定文章處理完畢。'
            return
        stage = 'read_board_range'
        stage_started = time.monotonic()
        newest = api.get_newest_index(PyPtt.NewIndex.BOARD, board=board)
        board_query_seconds = round(time.monotonic() - stage_started, 3)
        pending = db.execute('''SELECT first_index,last_index,direction FROM batch_ranges
                                WHERE site=? AND board=? AND completed=0''', (site, board)).fetchone()
        resumed = pending is not None and not config.get('new_batch')
        if resumed:
            start, original_end, direction = pending
            end = min(newest, original_end)
            expected_count = original_end - start + 1
        else:
            direction = config.get('direction', 'newest')
            expected_count = config['count']
            bounds = batch_bounds(newest, config['start'], expected_count, direction)
            start, end = bounds if bounds is not None else (1, 0)
            original_end = end
        if start > end:
            result = 'pending' if resumed else 'done'
            job.event('range', newest=newest, direction=direction, resumed=resumed,
                      planned_count=0, expected_count=expected_count, reason='no_range',
                      board_query_seconds=board_query_seconds)
            STATE['message'] = f'沒有可讀取的編號範圍，站上最新編號為 {newest}。'
            return
        stage = 'persist_batch'
        stage_started = time.monotonic()
        with db:
            if not resumed:
                db.execute('DELETE FROM batch_verified WHERE site=? AND board=?', (site, board))
            db.execute('''INSERT OR REPLACE INTO batch_ranges (site,board,first_index,last_index,direction)
                          VALUES(?,?,?,?,?)''', (site, board, start, original_end, direction))
        planned_count = end - start + 1
        job.event('range', start=start, end=end, direction=direction, resumed=resumed,
                  newest=newest, first_scan=end if direction == 'newest' else start,
                  last_scan=start if direction == 'newest' else end, planned_count=planned_count,
                  expected_count=expected_count, clipped_count=expected_count - planned_count,
                  saved_end=original_end, board_query_seconds=board_query_seconds)
        STATE.update(phase='download', batch_start=start, batch_end=end, scanned=0, batch_total=end-start+1,
                     direction=direction, resumed=resumed)
        indices = range(start, end + 1)
        for idx in (reversed(indices) if direction == 'newest' else indices):
            if STOP.is_set():
                result = 'stopped'
                STATE['message'] = '已停止；已完成的文章已保存。'
                break
            STATE['message'] = f'正在讀取編號 {idx}（{DIRECTIONS[direction]}）'
            article_started = stage_started = time.monotonic()
            query_seconds = None
            body_seconds = None
            details = {}
            added = False
            stage = 'query_article'
            aid = None
            query = api.get_post(board, index=idx, query=True)
            query_seconds = round(time.monotonic() - stage_started, 3)
            aid = query.get('aid')
            query_index = query.get('index')
            wrong_index = bool(query_index and query_index != idx)
            existing = stored_article(db, site, board, aid) if aid and not wrong_index else None
            locked = not wrong_index and query.get('is_lock') and (aid or query_index == idx)
            if confirmed_deleted(api, query, idx) or locked:
                status = 'locked' if locked else 'deleted'
                note_unavailable(db, site, board, idx, aid, status)
                outcome = status
            elif not aid or wrong_index:
                reason = '站方回傳的編號與要求不符' if wrong_index else '未取得 AID'
                record_failure(db, site, board, idx, reason)
                STATE['unresolved'] += 1
                show_issue(idx, aid, reason + '，可續傳重試')
                outcome = 'wrong_index' if wrong_index else 'no_aid'
                details['reason'] = reason
            elif existing and db.execute(
                    'SELECT 1 FROM batch_verified WHERE site=? AND board=? AND aid=?', (site, board, aid)).fetchone():
                with db:
                    db.execute('UPDATE articles SET source_index=? WHERE site=? AND board=? AND aid=?',
                               (idx, site, board, aid))
                    db.execute('DELETE FROM unavailable_articles WHERE site=? AND board=? AND (idx=? OR aid=?)',
                               (site, board, idx, aid))
                STATE['resumed_verified'] = STATE.get('resumed_verified', 0) + 1
                outcome = 'verified_earlier_in_batch'
            else:
                if STOP.wait(BEFORE_READ_SECONDS):
                    result = 'stopped'
                    STATE['message'] = '已停止；已完成的文章已保存。'
                    job.event('article', index=idx, aid=aid, outcome='stopped_before_read',
                              query_seconds=query_seconds, seconds=round(time.monotonic() - article_started, 3))
                    break
                stage = 'read_article'
                stage_started = time.monotonic()
                try:
                    outcome, _ = download_article(api, db, site, board, aid, source_index=idx,
                                                 existing=existing, checkpoint=True)
                except ArticleReadError as exc:
                    record_failure(db, site, board, idx, f'{aid} {exc}')
                    STATE['unresolved'] += 1
                    show_issue(idx, aid, str(exc))
                    outcome = 'unreadable'
                    details.update(reason=str(exc), capture=STATE['ansi_diagnostics'])
                else:
                    details.update(coverage=STATE.get('ansi_coverage', {}), capture=STATE['ansi_diagnostics'])
                    added = True
                body_seconds = round(time.monotonic() - stage_started, 3)
            STATE['scanned'] += 1
            job.event('article', index=idx, aid=aid, outcome=outcome, position=STATE['scanned'],
                      returned_index=query_index, had_backup=existing is not None,
                      query_seconds=query_seconds, body_seconds=body_seconds,
                      seconds=round(time.monotonic() - article_started, 3), **details)
            interrupted = STOP.wait(BETWEEN_ITEMS_SECONDS)
            if added and not interrupted:
                fresh_seconds += time.monotonic() - article_started
                STATE['timed_samples'] += 1
                STATE['seconds_per_article'] = round(fresh_seconds / STATE['timed_samples'], 1)
            STATE['elapsed'] = round(time.monotonic() - started)
            if interrupted and STATE['scanned'] < STATE['batch_total']:
                result = 'stopped'
                STATE['message'] = '已停止。下次登入同一看板會接回這批。'
                break
        else:
            if STATE['unresolved']:
                result = 'pending'
                STATE['message'] = '已掃完範圍，仍有文章待查。可登入續傳。'
            else:
                stage = 'complete_batch'
                stage_started = time.monotonic()
                with db:
                    db.execute('UPDATE batch_ranges SET completed=1 WHERE site=? AND board=?', (site, board))
                STATE['message'] = '本次範圍處理完畢。'
    except Exception as exc:
        # Never echo exception text: libraries may include screen/account data.
        name = type(exc).__name__
        result = 'disconnected' if name in ('ConnectionClosed', 'UseTooManyResources') else 'failed'
        if job:
            job.error(stage, exc, index=idx, aid=aid)
            job.event('stage_failed', stage=stage, seconds=round(time.monotonic() - stage_started, 3))
            if article_started is not None and stage in ('query_article', 'read_article'):
                job.event('article', index=idx, aid=aid, outcome='interrupted',
                          stage=stage, error=name, query_seconds=query_seconds,
                          seconds=round(time.monotonic() - article_started, 3),
                          capture=getattr(exc, 'capture_diagnostics', None))
        detail = str(exc) if isinstance(exc, ArticleReadError) else {
            'ConnectionClosed': '等網路恢復後，重新登入同一看板即可接續。',
            'UseTooManyResources': '站方限制本次連線，請稍後再試。',
            'TwoFactorAuthRequired': '此版尚未支援輸入二階段驗證碼。',
        }.get(name, '請查看程式 log 資料夾內的診斷紀錄。')
        STATE['message'] = f'已停止：{name}。{detail}'
        if db:
            try:
                record_failure(db, site, board, idx, name)
            except sqlite3.Error:
                STATE['message'] += ' 無法寫入失敗紀錄。'
    finally:
        config.clear()
        if api:
            logout_started = time.monotonic()
            logout_error = None
            try:
                api.logout()
            except Exception as exc:
                logout_error = type(exc).__name__
            if job:
                job.event('logout', seconds=round(time.monotonic() - logout_started, 3), error=logout_error)
        if db:
            if any(STATE.get(key) for key in ('saved', 'skipped', 'resumed_verified', 'kept_previous', 'unavailable')):
                message = STATE['message']
                STATE['message'] = '正在建立離線頁面…'
                export_started = time.monotonic()
                try:
                    count = publish_archive(db, folder)
                    if job:
                        job.event('export', seconds=round(time.monotonic() - export_started, 3), html_count=count)
                    STATE['message'] = message
                except Exception as exc:
                    if result == 'done':
                        result = 'failed'
                    STATE['message'] = f'{message}\n建立離線頁面失敗：{type(exc).__name__}。可使用「離線內容重整」。'
                    if job:
                        job.error('export', exc)
                        job.event('stage_failed', stage='export', seconds=round(time.monotonic() - export_started, 3))
            db.close()
        STATE['elapsed'] = round(time.monotonic() - started)
        if job:
            job.event('finish', elapsed=STATE['elapsed'], local_count=STATE.get('local_count'),
                      scanned=STATE.get('scanned', 0), updated=STATE.get('updated', 0),
                      planned_count=STATE.get('batch_total', 0),
                      remaining_scan=STATE.get('batch_total', 0) - STATE.get('scanned', 0),
                      pending_color=STATE.get('ansi_review', 0), timed_samples=STATE.get('timed_samples', 0),
                      average_seconds=STATE.get('seconds_per_article'),
                      saved=STATE['saved'], unchanged=STATE['skipped'],
                      resumed=STATE.get('resumed_verified', 0), unresolved=STATE['unresolved'],
                      unavailable=STATE.get('unavailable', 0), kept_previous=STATE.get('kept_previous', 0),
                      exported=STATE.get('exported', 0), result=result)
        STATE.update(running=False, phase='done', result=result)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self, code, body, kind='application/json'):
        data = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type',kind+'; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        if kind == 'text/html':
            self.send_header('Content-Security-Policy', "connect-src 'self'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
            return self.reply(403,'{}')
        if self.path in ('/', '/index.html'):
            html = (ROOT/'web.html').read_text(encoding='utf-8')
            return self.reply(200, html.replace('__TOKEN__', TOKEN).replace('__APP_VERSION__', APP_VERSION), 'text/html')
        if self.path == '/help.html':
            return self.reply(200,(ROOT/'help.html').read_text(encoding='utf-8'),'text/html')
        if self.path == '/status':
            return self.reply(200,json.dumps(STATE,ensure_ascii=False))
        if self.path == '/preferences':
            return self.reply(200,json.dumps(load_preferences(),ensure_ascii=False))
        reader = re.fullmatch(r'/reader/([A-Za-z0-9_-]+)/(index\.html|help\.html|articles/[0-9a-f]{64}\.html)', self.path)
        if reader and reader[1] in READERS:
            folder = READERS[reader[1]]
            path = (folder / reader[2]).resolve()
            if path.is_relative_to(folder) and path.is_file():
                return self.reply(200, path.read_text(encoding='utf-8'), 'text/html')
        self.reply(404,'{}')
    def do_POST(self):
        expected=f'http://127.0.0.1:{self.server.server_port}'
        if self.headers.get('Origin') != expected or self.headers.get('X-Token') != TOKEN:
            return self.reply(403,'{}')
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0 <= size <= 16384: raise ValueError()
            data=json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(data, dict):
                raise ValueError()
            if self.path == '/preferences':
                save_preferences(data)
                return self.reply(200,'{}')
            if self.path == '/stop':
                STOP.set()
                return self.reply(200,'{}')
            if self.path not in ('/start', '/export', '/pending'):
                return self.reply(404,'{}')
            folder = Path(data['folder']).expanduser()
            if not folder.is_absolute():
                raise ValueError()
            site, board = data.get('site'), data.get('board', '')
            if site not in SITES or not re.fullmatch(r'[A-Za-z0-9_-]{1,30}', board):
                raise ValueError()
            folder = folder.resolve()
            if folder.name.casefold() == f'{site}_{board}'.casefold() and not (folder / 'articles.sqlite3').exists():
                folder = folder.parent
            folder = archive_destination(folder, site, board)
            if self.path == '/pending':
                return self.reply(200, json.dumps({'folder': str(folder), 'pending': find_pending_range(
                    folder, site, board, with_progress=True)}))
            if self.path == '/start':
                data['folder'] = str(folder)
                mode = data.get('mode', 'new')
                if mode not in ('new', 'resume'):
                    raise ValueError()
                aid = '' if mode == 'resume' else data.get('aid', '')
                if not isinstance(aid, str):
                    raise ValueError()
                aid = aid.strip().removeprefix('#')
                if aid and not re.fullmatch(r'[A-Za-z0-9_-]{8}', aid):
                    raise ValueError()
                if aid and int(data.get('start') or 0) != 0:
                    raise ValueError()
                data['aid'] = aid
                data['new_batch'] = mode == 'new'
                if mode == 'new' and not aid:
                    data['direction'] = data.get('direction', 'newest')
                    data['count'] = int(data['count'])
                    data['start'] = int(data['start'])
                    if data['direction'] not in DIRECTIONS or not 1 <= data['count'] <= 10000 or data['start'] < 0:
                        raise ValueError()
                if not all(isinstance(data[key], str) and data[key] for key in ('user', 'password')):
                    raise ValueError()
                target, args = work, (data,)
            else:
                target, args = export_existing, (folder,)
            with LOCK:
                if STATE['running']: return self.reply(409,'{}')
                if self.path == '/start' and mode == 'resume' and not find_pending_range(folder, site, board):
                    return self.reply(409, '{"no_pending": true}')
                STATE.update(running=True, message='正在啟動…', saved=0, skipped=0,
                             unresolved=0, timed_samples=0, seconds_per_article=None, elapsed=0,
                             ansi_samples=0, ansi_colored=0, ansi_coverage={}, ansi_review=0, resumed=False, scanned=0,
                             batch_total=0, batch_start=None, batch_end=None, direction=None, phase='prepare',
                             exported=0, export_total=0, index_path=None, index_url=None, log_path=None,
                             resumed_verified=0, local_count=None, updated=0, issues=[], folder=str(folder),
                             unavailable=0, kept_previous=0, result=None)
                STOP.clear()
                threading.Thread(target=target,args=args,daemon=True).start()
            self.reply(200,json.dumps({'folder': str(folder)}))
        except (ValueError,KeyError,TypeError,sqlite3.Error,OSError): self.reply(400,'{}')

if __name__ == '__main__':
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    url=f'http://127.0.0.1:{server.server_port}'
    print('PTT / PTT2 downloader:',url,flush=True)
    webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: STOP.set()
