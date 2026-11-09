"""Local PTT and PTT2 downloader."""
import json
import logging
import secrets
import sqlite3
import threading
import time
import re
import webbrowser
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from archive import export_html

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
STATE = {'running': False, 'message': '尚未登入／實站驗證', 'saved': 0, 'skipped': 0}
LOCK = threading.Lock()
STOP = threading.Event()
logging.disable(logging.CRITICAL)


ARTICLE_TABLE = '''CREATE TABLE IF NOT EXISTS articles(
    site TEXT NOT NULL, board TEXT, aid TEXT, title TEXT, body TEXT, data TEXT,
    fetched REAL, source_index INTEGER, PRIMARY KEY(site,board,aid))'''
SITES = {'PTT': 'PTT1', 'PTT2': 'PTT2'}


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
    except Exception:
        db.close()
        raise
    return db


def save_article(db, board, post, *, site='PTT2', source_index=None):
    aid = post.get('aid')
    body = post.get('full_content')
    if not aid or not body or not post.get('pass_format_check'):
        raise ValueError('文章缺少 AID、全文或格式檢查未通過')
    db.execute('INSERT OR REPLACE INTO articles VALUES(?,?,?,?,?,?,?,?)',
               (site, board, aid, post.get('title',''), body,
                json.dumps(post,ensure_ascii=False), time.time(), source_index))
    db.commit()


def record_failure(db, site, board, index, reason):
    db.execute('INSERT INTO failures (site,board,idx,reason,created) VALUES(?,?,?,?,?)',
               (site, board, index, reason, time.time()))
    db.commit()


def export_existing(folder):
    try:
        if not (folder / 'articles.sqlite3').is_file():
            STATE['message'] = '找不到 articles.sqlite3，請選擇先前的備份資料夾。'
            return
        db = database(folder)
        try:
            count = export_html(db, folder)
        finally:
            db.close()
        STATE['message'] = f'已匯出 {count} 篇。請開啟 {folder / "index.html"}'
    except Exception as exc:
        STATE['message'] = f'匯出失敗：{type(exc).__name__}'
    finally:
        STATE['running'] = False


def work(config):
    api = None
    db = None
    idx = None
    board = config['board']
    site = config['site']
    fresh_seconds = 0.0
    started = time.monotonic()
    try:
        import PyPtt

        folder = Path(config['folder']).expanduser().resolve()
        db = database(folder)
        STATE['message'] = f'正在登入 {site}…'
        api = PyPtt.API(host=getattr(PyPtt.HOST, SITES[site]), log_level=PyPtt.LogLevel.SILENT,
                        check_update=False, verify_ssl=True)
        api.login(config.pop('user'), config.pop('password'), kick_other_session=False)
        newest = api.get_newest_index(PyPtt.NewIndex.BOARD, board=board)
        start = config['start'] or max(1, newest-config['count']+1)
        end = min(newest, start+config['count']-1)
        if start > end:
            STATE['message'] = f'沒有可讀取的編號範圍，站上最新編號為 {newest}。'
            return
        for idx in range(start, end+1):
            if STOP.is_set():
                STATE['message'] = '已停止；已完成的文章已保存。'
                break
            STATE['message'] = f'讀取編號 {idx} / {end}（本次範圍 {start}–{end}）'
            article_started = time.monotonic()
            added = False
            query = api.get_post(board, index=idx, query=True)
            aid = query.get('aid')
            if not aid:
                record_failure(db, site, board, idx, '沒有可用 AID，需人工核對')
                STATE['unresolved'] += 1
            elif db.execute('SELECT 1 FROM articles WHERE site=? AND board=? AND aid=?',
                            (site, board, aid)).fetchone():
                db.execute('UPDATE articles SET source_index=? WHERE site=? AND board=? AND aid=?',
                           (idx, site, board, aid))
                db.commit()
                STATE['skipped'] += 1
            else:
                if STOP.wait(2):
                    STATE['message'] = '已停止；已完成的文章已保存。'
                    break
                post = api.get_post(board, aid=aid)
                if post.get('aid') != aid:
                    raise ValueError('AID 不一致，停止避免存錯文章')
                save_article(db, board, post, site=site, source_index=idx)
                STATE['saved'] += 1
                added = True
            interrupted = STOP.wait(3)
            if added and not interrupted:
                fresh_seconds += time.monotonic() - article_started
                STATE['timed_samples'] += 1
                STATE['seconds_per_article'] = round(fresh_seconds / STATE['timed_samples'], 1)
            STATE['elapsed'] = round(time.monotonic() - started)
            if interrupted:
                STATE['message'] = '已停止；可重新掃描相同範圍，按 AID 略過已保存文章。'
                break
        else:
            STATE['message'] = '本次範圍處理完畢；不代表全板完整性已驗證。'
    except Exception as exc:
        # Never echo exception text: libraries may include screen/account data.
        name = type(exc).__name__
        STATE['message'] = f'已停止：{name}。若為 TwoFactorAuthRequired，第一版尚未支援驗證碼接續。'
        if db:
            try:
                record_failure(db, site, board, idx, name)
            except sqlite3.Error:
                STATE['message'] += ' 無法寫入失敗紀錄。'
    finally:
        config.clear()
        if api:
            try: api.logout()
            except Exception: pass
        if db:
            if STATE['saved'] or STATE['skipped']:
                message = STATE['message']
                STATE['message'] = '正在匯出離線頁面…'
                try:
                    count = export_html(db, folder)
                    STATE['message'] = f'{message}\n已匯出 {count} 篇。請開啟 {folder / "index.html"}'
                except Exception as exc:
                    STATE['message'] = f'{message}\n匯出失敗：{type(exc).__name__}，可重新匯出。'
            db.close()
        STATE['elapsed'] = round(time.monotonic() - started)
        STATE['running'] = False

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self, code, body, kind='application/json'):
        data = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type',kind+'; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        if kind == 'text/html':
            self.send_header('Content-Security-Policy', "connect-src 'self'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
            return self.reply(403,'{}')
        if self.path == '/':
            return self.reply(200,(ROOT/'web.html').read_text(encoding='utf-8').replace('__TOKEN__',TOKEN),'text/html')
        if self.path == '/status':
            return self.reply(200,json.dumps(STATE,ensure_ascii=False))
        self.reply(404,'{}')
    def do_POST(self):
        expected=f'http://127.0.0.1:{self.server.server_port}'
        if self.headers.get('Origin') != expected or self.headers.get('X-Token') != TOKEN:
            return self.reply(403,'{}')
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0 <= size <= 16384: raise ValueError()
            data=json.loads(self.rfile.read(size) or b'{}')
            if self.path == '/stop':
                STOP.set()
                return self.reply(200,'{}')
            if self.path not in ('/start', '/export'):
                return self.reply(404,'{}')
            folder = Path(data['folder']).expanduser()
            if not folder.is_absolute():
                raise ValueError()
            if self.path == '/start':
                if data['site'] not in SITES or not re.fullmatch(r'[A-Za-z0-9_-]{1,30}', data['board']):
                    raise ValueError()
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
                STATE.update(running=True, message='正在啟動…', saved=0, skipped=0,
                             unresolved=0, timed_samples=0, seconds_per_article=None, elapsed=0)
                STOP.clear()
                threading.Thread(target=target,args=args,daemon=True).start()
            self.reply(200,'{}')
        except (ValueError,KeyError,TypeError): self.reply(400,'{}')

if __name__ == '__main__':
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    url=f'http://127.0.0.1:{server.server_port}'
    print('PTT / PTT2 downloader:',url,flush=True)
    webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: STOP.set()
