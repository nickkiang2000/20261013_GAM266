"""Local PTT and PTT2 downloader."""
import errno
import json
import logging
import secrets
import sqlite3
import socket
import threading
import time
import re
import webbrowser
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from archive import export_html
import issue_records
from board_session import (ArticleStalled, BoardSession, BoardTemporarilyClosed, FALLBACK_HEIGHT, MIN_COMMAND_INTERVAL,
                           StationRateLimit, TAIPEI, detect_rate_limit)
from local_state import JobLog, load_preferences, load_rate_limits, save_preferences, save_rate_limits

ROOT = Path(__file__).resolve().parent
APP_VERSION = 'v2.16.0'
LOCAL_PORT = 18761
TOKEN = secrets.token_urlsafe(32)
STATE = {'running': False, 'message': '尚未登入／實站驗證', 'saved': 0, 'skipped': 0}
LOCK = threading.Lock()
STOP = threading.Event()
READERS = {}
RATE_LIMITS = load_rate_limits()
logging.disable(logging.CRITICAL)


ARTICLE_TABLE = '''CREATE TABLE IF NOT EXISTS articles(
    site TEXT NOT NULL, board TEXT, aid TEXT, title TEXT, body TEXT, data TEXT,
    fetched REAL, source_index INTEGER, PRIMARY KEY(site,board,aid))'''
SITES = {'PTT': 'PTT1', 'PTT2': 'PTT2'}
DIRECTIONS = {'newest': '新到舊', 'oldest': '舊到新'}
CAPTURE_MODES = {'line': '逐行', 'page': '自動擷取'}
BACKUP_MODES = {'recheck': '重新核對', 'fill': '補齊備份'}
BEFORE_READ_SECONDS = 2
BETWEEN_ITEMS_SECONDS = 3
ITEMS_BEFORE_REST = 25
REST_SECONDS = 20
BOARD_RECHECK_SECONDS = 180
# Increment when the stored capture format changes. HTML-only changes can be re-exported.
CAPTURE_VERSION = 1


class ArticleReadError(ValueError):
    """An individual article could not be saved safely."""


class DownloadStopped(Exception):
    """The user stopped an in-progress station wait."""


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
            if 'capture_mode' not in batch_columns:
                db.execute("ALTER TABLE batch_ranges ADD COLUMN capture_mode TEXT NOT NULL DEFAULT 'line'")
            if 'backup_mode' not in batch_columns:
                db.execute("ALTER TABLE batch_ranges ADD COLUMN backup_mode TEXT NOT NULL DEFAULT 'recheck'")
            db.execute('''CREATE TABLE IF NOT EXISTS batch_verified(
                          site TEXT, board TEXT, aid TEXT, PRIMARY KEY(site,board,aid))''')
            db.execute('''CREATE TABLE IF NOT EXISTS batch_unavailable(
                          site TEXT, board TEXT, idx INTEGER, aid TEXT, status TEXT,
                          PRIMARY KEY(site,board,idx))''')
            db.execute('''CREATE TABLE IF NOT EXISTS unavailable_articles(
                          site TEXT, board TEXT, idx INTEGER, aid TEXT, status TEXT,
                          PRIMARY KEY(site,board,idx))''')
            db.execute('''CREATE TABLE IF NOT EXISTS color_line_checks(
                          site TEXT, board TEXT, aid TEXT, idx INTEGER, matched INTEGER, total INTEGER,
                          PRIMARY KEY(site,board,aid))''')
            issue_records.initialize(db)
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


def record_failure(db, site, board, index, reason, *, article=False, aid=None):
    db.execute('INSERT INTO failures (site,board,idx,reason,created) VALUES(?,?,?,?,?)',
               (site, board, index, reason, time.time()))
    if article:
        issue_records.record(db, site, board, index, aid, reason)
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
        capture = 'capture_mode' if 'capture_mode' in columns else "'line'"
        backup = 'backup_mode' if 'backup_mode' in columns else "'recheck'"
        capture_mode, backup_mode = db.execute(f'SELECT {capture},{backup} FROM batch_ranges WHERE site=? AND board=?',
                                              (site, board)).fetchone()
        return {'first': first, 'last': last, 'direction': direction, 'reusable': reusable,
                'capture_mode': capture_mode, 'backup_mode': backup_mode}


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


def active_rate_limits(*, from_disk=False):
    now = datetime.now().astimezone()
    active = {}
    for limits in (RATE_LIMITS, load_rate_limits() if from_disk else {}):
        for site, limit in limits.items():
            if site not in SITES:
                continue
            try:
                deadline = datetime.fromisoformat(limit['until'])
                if deadline > now and (site not in active or deadline > datetime.fromisoformat(active[site]['until'])):
                    active[site] = limit
            except (TypeError, KeyError, ValueError):
                continue
    return active


def resume_checkpoints(db, site, board):
    rows = db.execute('''SELECT a.source_index, min(a.aid) FROM articles a
                         JOIN batch_verified b USING(site,board,aid)
                         WHERE a.site=? AND a.board=?
                         GROUP BY a.source_index HAVING count(*)=1''', (site, board))
    checkpoints = {index: (aid, 'saved') for index, aid in rows if index is not None}
    skipped = db.execute('SELECT idx,aid,status FROM batch_unavailable WHERE site=? AND board=?', (site, board))
    checkpoints.update({index: (aid, status) for index, aid, status in skipped})
    return checkpoints


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


def board_screen_diagnostic(api):
    """Classify the last screen without persisting any terminal text."""
    from PyPtt import _api_util, screens

    core = api.connect_core
    queue = core.get_screen_queue()
    screen = queue[-1] if queue else ''
    rows = screen.splitlines()
    strict_header = any('作  者' in row and '日 期' in row for row in rows)
    flexible_header = any(re.search(r'作\s*者', row) and re.search(r'日\s*期', row)
                          for row in rows)
    if screens.in_board_setting(screen):
        kind = 'board_settings'
    elif _api_util.is_article_list(api, screen) or flexible_header:
        kind = 'article_list'
    elif any(re.match(r'^\s*[★※＊!！]*\s*(?:您|你)?(?:沒有權限|無權|不允許|無法)進入(?:此|本|該|這個)?看板', row)
             for row in rows):
        kind = 'access_notice'
    elif any(re.match(r'^\s*[★※＊!！]*\s*(?:查無此看板|無此看板|找不到(?:此|該)看板|看板不存在)', row)
             for row in rows):
        kind = 'missing_board_notice'
    elif any('主功能表' in row for row in rows[:5]):
        kind = 'main_menu'
    else:
        kind = 'unrecognized' if rows else 'empty'
    return {'screen_kind': kind, 'screen_rows': len(rows),
            'strict_board_header': strict_header, 'flexible_board_header': flexible_header,
            'silent_timeout': getattr(core, 'last_timeout_was_silent', False) is True}


def note_unavailable(db, site, board, index, aid, status, *, checkpoint=False):
    with db:
        issue_records.resolve(db, site, board, index, aid)
        if index is not None:
            db.execute('INSERT OR REPLACE INTO unavailable_articles VALUES(?,?,?,?,?)',
                       (site, board, index, aid, status))
            if checkpoint:
                db.execute('INSERT OR REPLACE INTO batch_unavailable VALUES(?,?,?,?,?)',
                           (site, board, index, aid, status))
    STATE['unavailable'] = STATE.get('unavailable', 0) + 1
    label = {'deleted': '（被刪除）', 'locked': '（已鎖定）', 'empty': '（站上無正文）'}[status]
    show_issue(index, aid, label)


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


def test_destination(site, board, aid, capture_mode, short_terminal):
    height = FALLBACK_HEIGHT if short_terminal else 100
    return (ROOT / 'test-downloads' / f'{site}_{board}' / aid
            / f'{capture_mode or "line"}-{height}' / ('run-' + secrets.token_hex(6)))


def export_progress(done, total):
    STATE.update(exported=done, export_total=total)


def publish_archive(db, folder, job=None, *, force=False):
    STATE['phase'] = 'export'
    publish_color_checks(db, folder)
    stats = {}
    count = export_html(db, folder, progress=export_progress, force=force, stats=stats)
    STATE['html_written'] = stats['written']
    if job:
        for site, board, index, aid, reason, attempts in db.execute(
                'SELECT site,board,idx,aid,reason,attempts FROM article_issues ORDER BY site,board,idx,aid'):
            job.event('offline_issue', site=site, board=board, index=index, aid=aid,
                      reason=reason, attempts=attempts)
        job.event('offline_records', unreadable=db.execute('SELECT count(*) FROM article_issues').fetchone()[0],
                  color_checks=db.execute('SELECT count(*) FROM color_line_checks').fetchone()[0],
                  unavailable=db.execute('SELECT count(*) FROM unavailable_articles').fetchone()[0])
    STATE['index_path'] = str(folder / 'index.html')
    reader = secrets.token_urlsafe(24)
    READERS[reader] = folder.resolve()
    STATE['index_url'] = f'/reader/{reader}/index.html'
    return count


def export_existing(folder):
    job = None
    result = 'failed'
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
            count = publish_archive(db, folder, job=job, force=True)
        STATE['message'] = '離線內容重整完成。'
        result = 'done'
        job.event('finish', exported=count, html_written=STATE['html_written'])
    except Exception as exc:
        STATE['message'] = f'離線內容重整失敗：{type(exc).__name__}'
        if job:
            job.error('refresh_offline', exc)
    finally:
        STATE.update(running=False, phase='done', result=result)


def complete_capture(post):
    coverage = post.get('ansi_coverage') or {}
    return (post.get('capture_version') == CAPTURE_VERSION and coverage.get('total', 0) > 0
            and coverage.get('matched') == coverage['total'])


def download_article(api, db, site, board, aid, *, source_index=None, existing=None, checkpoint=False):
    from ansi_capture import read_colored_post

    read_started = time.monotonic()
    try:
        post = read_colored_post(api, board, aid)
    except ArticleStalled as exc:
        # Only formal page-first jobs retry, and only after a verified return to the list.
        if (not checkpoint or source_index is None or getattr(api, 'capture_mode', None) != 'page'
                or getattr(api, 'view', None) != 'list'):
            raise
        failed_page = {'reason': str(exc), 'capture': getattr(exc, 'capture_diagnostics', {}),
                       'page_seconds': round(time.monotonic() - read_started, 3)}
        if STOP.is_set():
            raise DownloadStopped() from exc
        STATE['message'] = f'編號 {source_index} 逐頁未通過，正在改用逐行重讀'
        retry_started = time.monotonic()
        try:
            api.capture_mode = 'line'
            current = api.get_post(board, index=source_index, query=True)
            if current.get('index') != source_index or current.get('aid') != aid or current.get('is_lock'):
                raise ArticleReadError('改用逐行前無法確認仍為同一篇可讀文章')
            post = read_colored_post(api, board, aid)
        except Exception as retry_error:
            failed_page.update(line_result='failed', line_seconds=round(time.monotonic() - retry_started, 3))
            retry_error.capture_diagnostics = dict(getattr(retry_error, 'capture_diagnostics', {}),
                                                   page_fallback=failed_page)
            raise
        finally:
            api.capture_mode = 'page'
        failed_page.update(line_result='read', line_seconds=round(time.monotonic() - retry_started, 3))
        post.setdefault('ansi_diagnostics', {})['page_fallback'] = failed_page
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
        note_unavailable(db, site, board, source_index, aid, 'locked', checkpoint=checkpoint)
        return 'locked', False
    if post.get('no_content'):
        STATE['ansi_coverage'] = {}
        note_unavailable(db, site, board, source_index, aid, 'empty', checkpoint=checkpoint)
        return 'empty', False
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
        issue_records.resolve(db, site, board, source_index, aid)
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
            db.execute('DELETE FROM batch_unavailable WHERE site=? AND board=? AND idx=?',
                       (site, board, source_index))
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


def record_rate_limit(site, limit, job):
    RATE_LIMITS[site] = limit
    job.event('rate_limit', **limit)
    try:
        save_rate_limits(active_rate_limits(from_disk=True))
    except OSError as exc:
        job.error('save_rate_limit', exc)


def station_action(session, board, site, job, action, *, retry=None):
    try:
        return action()
    except (StationRateLimit, BoardTemporarilyClosed) as exc:
        # Only wait when the terminal itself recognized the prompt and saved its state.
        if session.notice is not exc:
            raise
        limited = isinstance(exc, StationRateLimit)
        if limited:
            record_rate_limit(site, exc.details, job)
            until = datetime.fromisoformat(exc.details['until'])
        else:
            until = datetime.now(TAIPEI) + timedelta(seconds=BOARD_RECHECK_SECONDS)
        kind = 'rate_limit' if limited else 'board_closed'
        phase = STATE.get('phase', 'prepare')
        label = '站方限速' if limited else '看板暫時關閉'
        STATE.update(phase='station_wait', wait_kind=kind, wait_until=until.isoformat(timespec='seconds'),
                     message=f'{label}，保留連線等到 {until:%m/%d %H:%M} 再確認一次。')
        job.event('station_wait', kind=kind, until=STATE['wait_until'])
        try:
            if not session.recover_notice(board, until, STOP):
                raise DownloadStopped()
        finally:
            STATE.update(phase=phase, wait_kind=None, wait_until=None)
        job.event('station_recovered', kind=kind)
        STATE['message'] = '站方畫面已恢復，正在接續原批次。'
        return (retry or action)()


def work(config):
    api = None
    session = None
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
    rest_count = 0
    rest_seconds = 0.0
    try:
        import PyPtt

        folder = Path(config['folder']).expanduser().resolve()
        STATE['folder'] = str(folder)
        db = database(folder)
        STATE['local_count'] = db.execute('SELECT count(*) FROM articles').fetchone()[0]
        job = JobLog()
        STATE['log_path'] = str(job.path)
        mode = config.get('mode', 'new')
        if config.get('aid') and mode != 'test':
            mode = 'single'
        capture_mode = (config.get('capture_mode') or 'line') if config.get('aid') else 'page'
        backup_mode = config.get('backup_mode', 'recheck')
        height = FALLBACK_HEIGHT if config.get('aid') and config.get('short_terminal') else 100
        all_articles = mode == 'new' and config.get('all_articles', False)
        job.event('start', version=APP_VERSION, site=site, board=board, capture_version=CAPTURE_VERSION,
                  mode=mode, requested_aid=config.get('aid') or None,
                  requested_start=config.get('start') if mode == 'new' else None,
                  requested_count=config.get('count') if mode == 'new' else None,
                  requested_direction=config.get('direction', 'newest') if mode == 'new' else None,
                  requested_capture_mode=capture_mode,
                  backup_mode=backup_mode if mode == 'new' else None,
                  all_articles=all_articles, terminal_height=height,
                  local_count=STATE['local_count'], before_read_seconds=BEFORE_READ_SECONDS,
                  between_items_seconds=BETWEEN_ITEMS_SECONDS, min_command_seconds=MIN_COMMAND_INTERVAL,
                  items_before_rest=ITEMS_BEFORE_REST, rest_seconds=REST_SECONDS)
        stage = 'login'
        stage_started = time.monotonic()
        STATE['message'] = f'正在登入 {site}…'
        api = PyPtt.API(host=getattr(PyPtt.HOST, SITES[site]), log_level=PyPtt.LogLevel.SILENT,
                        check_update=False, verify_ssl=True, screen_height=height)
        session = BoardSession(api)
        session.capture_mode = capture_mode
        def check_stop():
            if STOP.is_set():
                raise DownloadStopped()
        session.check_stop = check_stop
        api.login(config.pop('user'), config.pop('password'), kick_other_session=False)
        job.event('login', seconds=round(time.monotonic() - stage_started, 3))
        def run_station(action, *, retry=None):
            return station_action(session, board, site, job, action, retry=retry)

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
                outcome, _ = run_station(lambda: download_article(
                    session, db, site, board, aid, existing=stored_article(db, site, board, aid)))
            except (ArticleReadError, ArticleStalled) as exc:
                result = 'pending'
                STATE.update(scanned=1, unresolved=1)
                show_issue(None, aid, str(exc))
                job.event('article', aid=aid, outcome='unreadable', reason=str(exc),
                          capture=getattr(exc, 'capture_diagnostics', STATE.get('ansi_diagnostics')),
                          stalled_line=getattr(exc, 'line', None), seconds=round(time.monotonic() - article_started, 3))
                STATE['message'] = '指定文章待查。'
                record_failure(db, site, board, None, str(exc), article=True, aid=aid)
                return
            job.event('article', aid=aid, outcome=outcome, coverage=STATE.get('ansi_coverage', {}),
                      capture=STATE['ansi_diagnostics'], seconds=round(time.monotonic() - article_started, 3))
            STATE['scanned'] = 1
            STATE['message'] = '指定文章處理完畢。'
            return
        stage = 'read_board_range'
        stage_started = time.monotonic()
        newest = run_station(lambda: session.get_newest_index(PyPtt.NewIndex.BOARD, board=board))
        board_query_seconds = round(time.monotonic() - stage_started, 3)
        pending = db.execute('''SELECT first_index,last_index,direction,backup_mode FROM batch_ranges
                                WHERE site=? AND board=? AND completed=0''', (site, board)).fetchone()
        resumed = pending is not None and not config.get('new_batch')
        if resumed:
            start, original_end, direction, backup_mode = pending
            end = min(newest, original_end)
            expected_count = original_end - start + 1
        else:
            direction = config.get('direction', 'newest')
            expected_count = newest if all_articles else config['count']
            bounds = (1, newest) if all_articles else batch_bounds(newest, config['start'], expected_count, direction)
            start, end = bounds if bounds is not None else (1, 0)
            original_end = end
        if start > end:
            result = 'pending' if resumed else 'failed'
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
                db.execute('DELETE FROM batch_unavailable WHERE site=? AND board=?', (site, board))
            db.execute('''INSERT OR REPLACE INTO batch_ranges
                          (site,board,first_index,last_index,direction,capture_mode,backup_mode)
                          VALUES(?,?,?,?,?,?,?)''', (site, board, start, original_end, direction,
                                                    session.capture_mode, backup_mode))
        planned_count = end - start + 1
        job.event('range', start=start, end=end, direction=direction, resumed=resumed,
                  capture_mode=session.capture_mode, backup_mode=backup_mode,
                  newest=newest, first_scan=end if direction == 'newest' else start,
                  last_scan=start if direction == 'newest' else end, planned_count=planned_count,
                  expected_count=expected_count, clipped_count=expected_count - planned_count,
                  saved_end=original_end, board_query_seconds=board_query_seconds)
        STATE.update(phase='download', batch_start=start, batch_end=end, scanned=0, batch_total=end-start+1,
                     direction=direction, resumed=resumed)
        indices = range(end, start - 1, -1) if direction == 'newest' else range(start, end + 1)
        checkpoints = resume_checkpoints(db, site, board) if resumed else {}
        checked_until = skip_until = items_since_rest = 0
        for position, idx in enumerate(indices):
            if STOP.is_set():
                result = 'stopped'
                STATE['message'] = '已停止；已完成的文章已保存。'
                break
            if position < skip_until:
                continue
            if items_since_rest >= ITEMS_BEFORE_REST:
                stage = 'pacing_rest'
                stage_started = time.monotonic()
                STATE['message'] = f'已處理 {items_since_rest} 筆，休息 {REST_SECONDS} 秒後自動繼續。'
                job.event('pacing_rest', next_index=idx, scanned=STATE['scanned'],
                          scheduled_seconds=REST_SECONDS, operations=dict(session.operations))
                interrupted = STOP.wait(REST_SECONDS)
                rest_count += 1
                rest_seconds += time.monotonic() - stage_started
                items_since_rest = 0
                if interrupted:
                    result = 'stopped'
                    STATE['message'] = '已中止。下次登入同一看板會接回這批。'
                    break
            if position >= checked_until and idx in checkpoints:
                checked_until = position + 1
                while checked_until < len(indices) and indices[checked_until] in checkpoints:
                    checked_until += 1
                anchor_index = indices[checked_until - 1]
                anchor_aid, status = checkpoints[anchor_index]
                stage = 'resume_anchor'
                stage_started = time.monotonic()
                aid = anchor_aid
                idx = anchor_index
                anchor = run_station(lambda: session.get_post(board, index=anchor_index, query=True))
                idx = indices[position]
                matches_aid = anchor.get('aid') == anchor_aid and anchor.get('index') in (None, anchor_index)
                if status == 'deleted':
                    matched = confirmed_deleted(session, anchor, anchor_index)
                else:
                    matched = matches_aid and bool(anchor.get('is_lock')) == (status == 'locked')
                candidates = checked_until - position
                job.event('resume_anchor', index=anchor_index, aid=anchor_aid, matched=matched,
                          candidates=candidates, status=status)
                if matched:
                    skipped = sum(checkpoints[index][1] != 'saved' for index in indices[position:checked_until])
                    STATE['scanned'] += candidates
                    STATE['resumed_verified'] = STATE.get('resumed_verified', 0) + candidates - skipped
                    STATE['unavailable'] = STATE.get('unavailable', 0) + skipped
                    skip_until = checked_until
                    continue
            STATE['message'] = f'正在讀取編號 {idx}（{DIRECTIONS[direction]} · {CAPTURE_MODES[session.capture_mode]}）'
            article_started = stage_started = time.monotonic()
            query_seconds = None
            body_seconds = None
            details = {}
            added = False
            stage = 'query_article'
            aid = None
            query = run_station(lambda: session.get_post(board, index=idx, query=True))
            query_seconds = round(time.monotonic() - stage_started, 3)
            aid = query.get('aid')
            query_index = query.get('index')
            wrong_index = bool(query_index and query_index != idx)
            existing = stored_article(db, site, board, aid) if aid and not wrong_index else None
            locked = not wrong_index and query.get('is_lock') and (aid or query_index == idx)
            if confirmed_deleted(session, query, idx) or locked:
                status = 'locked' if locked else 'deleted'
                note_unavailable(db, site, board, idx, aid, status, checkpoint=True)
                outcome = status
            elif not aid or wrong_index:
                reason = '站方回傳的編號與要求不符' if wrong_index else '未取得 AID'
                record_failure(db, site, board, idx, reason, article=True)
                STATE['unresolved'] += 1
                show_issue(idx, aid, reason + '，可續傳重試')
                outcome = 'wrong_index' if wrong_index else 'no_aid'
                details['reason'] = reason
            elif existing and ((backup_mode == 'fill' and existing.get('full_content')) or db.execute(
                    'SELECT 1 FROM batch_verified WHERE site=? AND board=? AND aid=?', (site, board, aid)).fetchone()):
                with db:
                    if backup_mode == 'recheck':
                        issue_records.resolve(db, site, board, idx, aid)
                    db.execute('UPDATE articles SET source_index=? WHERE site=? AND board=? AND aid=?',
                               (idx, site, board, aid))
                    db.execute('INSERT OR IGNORE INTO batch_verified VALUES(?,?,?)', (site, board, aid))
                    db.execute('DELETE FROM unavailable_articles WHERE site=? AND board=? AND (idx=? OR aid=?)',
                               (site, board, idx, aid))
                    db.execute('DELETE FROM batch_unavailable WHERE site=? AND board=? AND idx=?',
                               (site, board, idx))
                if backup_mode == 'fill':
                    STATE['reused_local'] = STATE.get('reused_local', 0) + 1
                    outcome = 'reused_local'
                else:
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
                def read_article():
                    return download_article(session, db, site, board, aid, source_index=idx,
                                            existing=existing, checkpoint=True)

                def retry_article():
                    current = session.get_post(board, index=idx, query=True)
                    if current.get('aid') != aid:
                        raise ArticleReadError('恢復後文章代碼已變動，保留待查')
                    return read_article()

                try:
                    outcome, _ = run_station(read_article, retry=retry_article)
                except (ArticleReadError, ArticleStalled) as exc:
                    record_failure(db, site, board, idx, str(exc), article=True, aid=aid)
                    STATE['unresolved'] += 1
                    show_issue(idx, aid, str(exc))
                    outcome = 'unreadable'
                    details.update(reason=str(exc), capture=getattr(exc, 'capture_diagnostics',
                                                                     STATE.get('ansi_diagnostics')))
                    if isinstance(exc, ArticleStalled):
                        details['stalled_line'] = exc.line
                else:
                    details.update(coverage=STATE.get('ansi_coverage', {}), capture=STATE['ansi_diagnostics'])
                    added = True
                body_seconds = round(time.monotonic() - stage_started, 3)
            STATE['scanned'] += 1
            items_since_rest += 1
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
    except DownloadStopped:
        result = 'stopped'
        STATE['message'] = '已中止，已完成的進度已保存。'
    except Exception as exc:
        # Never echo exception text: libraries may include screen/account data.
        name = type(exc).__name__
        result = 'disconnected' if name == 'ConnectionClosed' else 'failed'
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
            'BoardNavigationError': '終端位置無法確認，已停止送鍵。原批次可稍後續傳。',
            'BoardTemporarilyClosed': '看板仍暫時關閉，已保留進度。',
            'TwoFactorAuthRequired': '此版尚未支援輸入二階段驗證碼。',
        }.get(name, '請查看程式 log 資料夾內的診斷紀錄。')
        if isinstance(exc, StationRateLimit) or name == 'UseTooManyResources':
            result = 'rate_limited'
            limit = exc.details if isinstance(exc, StationRateLimit) else detect_rate_limit('程式耗用過多')
            if job:
                record_rate_limit(site, limit, job)
            until = datetime.fromisoformat(limit['until']).strftime('%m/%d %H:%M')
            detail = f'已保留批次並停止送鍵。最早可在 {until} 後手動續傳。'
        if name == 'NoSuchBoard' and api is not None:
            try:
                diagnostic = board_screen_diagnostic(api)
            except Exception as diagnostic_error:
                if job:
                    job.error('board_diagnostic', diagnostic_error)
                diagnostic = {}
            if job:
                job.event('board_diagnostic', stage=stage, **diagnostic)
            detail = {
                'article_list': '收到類似文章列表的畫面，但 SDK 未能完成看板辨識。請手動確認看板可讀。',
                'access_notice': '終端出現看板進入限制提示，請用同一帳號手動確認站方訊息。',
                'missing_board_notice': '終端出現找不到看板的提示，請確認站台、板名及閱讀權限。',
            }.get(diagnostic.get('screen_kind'),
                  '已登入，但未能辨識看板畫面。請用同一帳號手動確認能否進板，再按登入續傳。')
        STATE['message'] = f'站方限速。{detail}' if result == 'rate_limited' else f'已停止：{name}。{detail}'
        if db:
            try:
                record_failure(db, site, board, idx, name + '：' + detail,
                               article=stage in ('query_article', 'read_article'), aid=aid)
            except sqlite3.Error:
                STATE['message'] += ' 無法寫入失敗紀錄。'
    finally:
        config.clear()
        if api:
            logout_started = time.monotonic()
            logout_error = None
            try:
                api.connect_core.close()
            except Exception as exc:
                logout_error = type(exc).__name__
            if job:
                job.event('logout', method='transport_close',
                          seconds=round(time.monotonic() - logout_started, 3), error=logout_error)
        if db:
            if (any(STATE.get(key) for key in ('saved', 'skipped', 'resumed_verified', 'reused_local',
                                               'kept_previous', 'unavailable', 'unresolved'))
                    or db.execute('SELECT 1 FROM article_issues LIMIT 1').fetchone()):
                message = STATE['message']
                STATE['message'] = '正在建立離線頁面…'
                export_started = time.monotonic()
                try:
                    count = publish_archive(db, folder, job=job)
                    if job:
                        job.event('export', seconds=round(time.monotonic() - export_started, 3), html_count=count,
                                  html_written=STATE['html_written'])
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
            if session:
                job.event('terminal_operations', **dict(session.operations))
            job.event('finish', elapsed=STATE['elapsed'], local_count=STATE.get('local_count'),
                      scanned=STATE.get('scanned', 0), updated=STATE.get('updated', 0),
                      planned_count=STATE.get('batch_total', 0),
                      remaining_scan=STATE.get('batch_total', 0) - STATE.get('scanned', 0),
                      pending_color=STATE.get('ansi_review', 0), timed_samples=STATE.get('timed_samples', 0),
                      rest_count=rest_count, rest_seconds=round(rest_seconds, 3),
                      average_seconds=STATE.get('seconds_per_article'),
                      saved=STATE['saved'], unchanged=STATE['skipped'],
                      reused_local=STATE.get('reused_local', 0),
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
            return self.reply(200,json.dumps({**STATE, 'rate_limits': active_rate_limits()},ensure_ascii=False))
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
            site, board = data.get('site'), data.get('board', '')
            if site not in SITES or not re.fullmatch(r'[A-Za-z0-9_-]{1,30}', board):
                raise ValueError()
            mode = data.get('mode', 'new') if self.path == '/start' else None
            if mode != 'test':
                folder = Path(data['folder']).expanduser()
                if not folder.is_absolute():
                    raise ValueError()
                folder = folder.resolve()
                if folder.name.casefold() == f'{site}_{board}'.casefold() and not (folder / 'articles.sqlite3').exists():
                    folder = folder.parent
                folder = archive_destination(folder, site, board)
            if self.path == '/pending':
                return self.reply(200, json.dumps({'folder': str(folder), 'pending': find_pending_range(
                    folder, site, board, with_progress=True)}))
            if self.path == '/start':
                if mode not in ('new', 'resume', 'test'):
                    raise ValueError()
                aid = ''
                if mode == 'test':
                    aid = data['aid'].strip().removeprefix('#') if isinstance(data['aid'], str) else ''
                    if not re.fullmatch(r'[A-Za-z0-9_-]{8}', aid):
                        raise ValueError()
                data['aid'] = aid
                data['new_batch'] = mode == 'new'
                data['short_terminal'] = bool(aid) and data.get('short_terminal') == 'on'
                if mode == 'test':
                    capture_mode = data.get('capture_mode') or 'line'
                    if capture_mode not in ('line', 'page'):
                        raise ValueError()
                    data['capture_mode'] = capture_mode
                    folder = test_destination(site, board, aid, capture_mode, data['short_terminal'])
                data['folder'] = str(folder)
                data['all_articles'] = mode == 'new' and data.get('all_articles') == 'on'
                if mode == 'new':
                    data['backup_mode'] = data.get('backup_mode', 'fill')
                    if data['backup_mode'] not in BACKUP_MODES:
                        raise ValueError()
                    data['direction'] = data.get('direction', 'newest')
                    if data['direction'] not in DIRECTIONS:
                        raise ValueError()
                    if data['all_articles']:
                        data['count'] = data['start'] = None
                    else:
                        data['count'] = int(data['count'])
                        data['start'] = int(data['start'])
                        if not 1 <= data['count'] <= 10000 or data['start'] < 0:
                            raise ValueError()
                if not all(isinstance(data[key], str) and data[key] for key in ('user', 'password')):
                    raise ValueError()
                target, args = work, (data,)
            else:
                target, args = export_existing, (folder,)
            with LOCK:
                if STATE['running']: return self.reply(409,'{}')
                if self.path == '/start':
                    limit = active_rate_limits(from_disk=True).get(site)
                    if limit:
                        RATE_LIMITS[site] = limit
                        return self.reply(429, json.dumps({'rate_limit': limit}))
                if self.path == '/start' and mode == 'resume' and not find_pending_range(folder, site, board):
                    return self.reply(409, '{"no_pending": true}')
                STATE.update(running=True, message='正在啟動…', saved=0, skipped=0,
                             unresolved=0, timed_samples=0, seconds_per_article=None, elapsed=0,
                             ansi_samples=0, ansi_colored=0, ansi_coverage={}, ansi_review=0, resumed=False, scanned=0,
                             batch_total=0, batch_start=None, batch_end=None, direction=None, phase='prepare',
                             exported=0, export_total=0, index_path=None, index_url=None, log_path=None,
                             resumed_verified=0, local_count=None, updated=0, issues=[], folder=str(folder),
                             unavailable=0, kept_previous=0, reused_local=0, html_written=0,
                             result=None, wait_kind=None, wait_until=None,
                             task_id=secrets.token_urlsafe(12),
                             operation=('test' if mode == 'test' else 'download') if self.path == '/start' else 'export')
                STOP.clear()
                threading.Thread(target=target,args=args,daemon=True).start()
            self.reply(200,json.dumps({'folder': str(folder)}))
        except (ValueError,KeyError,TypeError,sqlite3.Error,OSError): self.reply(400,'{}')

class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = not hasattr(socket, 'SO_EXCLUSIVEADDRUSE')

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server():
    # Browsers remember notification permission per origin, including the port.
    try:
        return LocalHTTPServer(('127.0.0.1', LOCAL_PORT), Handler)
    except OSError as exc:
        if exc.errno not in (errno.EADDRINUSE, errno.EACCES) and getattr(exc, 'winerror', None) not in (10048, 10013):
            raise
        return LocalHTTPServer(('127.0.0.1', 0), Handler)


if __name__ == '__main__':
    server=create_server()
    url=f'http://127.0.0.1:{server.server_port}'
    print('PTT / PTT2 downloader:',url,flush=True)
    webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: STOP.set()
