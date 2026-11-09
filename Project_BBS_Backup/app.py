"""Local PTT2 sample downloader. No LLM calls; credentials remain in memory."""
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

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
STATE = {'running': False, 'message': '尚未登入／實站驗證', 'saved': 0, 'skipped': 0}
LOCK = threading.Lock()
STOP = threading.Event()
logging.disable(logging.CRITICAL)

def database(folder):
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / 'articles.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS articles(board TEXT, aid TEXT, title TEXT, body TEXT, data TEXT, fetched REAL, PRIMARY KEY(board,aid))')
    db.execute('CREATE TABLE IF NOT EXISTS failures(board TEXT, idx INTEGER, reason TEXT, created REAL)')
    return db

def save_article(db, board, post):
    aid = post.get('aid')
    body = post.get('full_content')
    if not aid or not body or not post.get('pass_format_check'):
        raise ValueError('文章缺少 AID、全文或格式檢查未通過')
    db.execute('INSERT OR REPLACE INTO articles VALUES(?,?,?,?,?,?)',
               (board, aid, post.get('title',''), body, json.dumps(post,ensure_ascii=False), time.time()))
    db.commit()

def work(config):
    import PyPtt
    api = None
    db = None
    idx = 0
    try:
        folder = Path(config['folder']).expanduser().resolve()
        db = database(folder)
        STATE.update(message='正在登入 PTT2…', saved=0, skipped=0)
        api = PyPtt.API(host=PyPtt.HOST.PTT2, log_level=PyPtt.LogLevel.SILENT,
                        check_update=False, verify_ssl=True)
        api.login(config.pop('user'), config.pop('password'), kick_other_session=False)
        board = config['board']
        newest = api.get_newest_index(PyPtt.NewIndex.BOARD, board=board)
        start = config['start'] or max(1, newest-config['count']+1)
        end = min(newest, start+config['count']-1)
        for idx in range(start, end+1):
            if STOP.is_set():
                STATE['message'] = '已停止；已完成的文章已保存。'
                break
            STATE['message'] = f'讀取編號 {idx} / {end}（本次範圍 {start}–{end}）'
            query = api.get_post(board, index=idx, query=True)
            aid = query.get('aid')
            if not aid:
                db.execute('INSERT INTO failures VALUES(?,?,?,?)',(board,idx,'沒有可用 AID，可能已刪除；需人工核對',time.time()))
                db.commit()
                STATE['skipped'] += 1
            elif db.execute('SELECT 1 FROM articles WHERE board=? AND aid=?',(board,aid)).fetchone():
                STATE['skipped'] += 1
            else:
                if STOP.wait(2):
                    break
                post = api.get_post(board, aid=aid)
                if post.get('aid') != aid:
                    raise ValueError('AID 不一致，停止避免存錯文章')
                save_article(db,board,post)
                STATE['saved'] += 1
            if STOP.wait(3):
                STATE['message'] = '已停止；可重新掃描相同範圍，按 AID 略過已保存文章。'
                break
        else:
            STATE['message'] = '本次範圍處理完畢；不代表全板完整性已驗證。'
        export_html(db, folder)
    except Exception as exc:
        # Never echo exception text: libraries may include screen/account data.
        name = type(exc).__name__
        STATE['message'] = f'已停止：{name}。若為 TwoFactorAuthRequired，第一版尚未支援驗證碼接續。'
        if db:
            db.execute('INSERT INTO failures VALUES(?,?,?,?)',(config['board'],idx,name,time.time()))
            db.commit()
            export_html(db, folder)
    finally:
        config.clear()
        if api:
            try: api.logout()
            except Exception: pass
        if db: db.close()
        STATE['running'] = False

def export_html(db, folder):
    import html
    rows = db.execute('SELECT title,body FROM articles ORDER BY fetched').fetchall()
    blocks = ''.join('<details><summary>'+html.escape(t or '(無標題)')+'</summary><pre>'+html.escape(b)+'</pre></details>' for t,b in rows)
    content = '<!doctype html><meta charset="utf-8"><title>PTT2 離線文章</title><style>body{max-width:1000px;margin:40px auto;font-family:system-ui}pre{white-space:pre-wrap}details{padding:12px;border-bottom:1px solid #ccc}</style><h1>PTT2 離線文章</h1><p>共 '+str(len(rows))+' 篇；純文字呈現，ANSI 色彩尚未還原。</p>'+blocks
    temp = folder / 'index.html.tmp'
    temp.write_text(content,encoding='utf-8')
    temp.replace(folder / 'index.html')

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
            if self.path != '/start': return self.reply(404,'{}')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,30}',data['board']): raise ValueError()
            data['count']=int(data['count']); data['start']=int(data['start'])
            if not 1 <= data['count'] <= 10000 or data['start'] < 0: raise ValueError()
            if not data['user'] or not data['password'] or not data['folder']: raise ValueError()
            with LOCK:
                if STATE['running']: return self.reply(409,'{}')
                STATE['running']=True
                STOP.clear()
                threading.Thread(target=work,args=(data,),daemon=True).start()
            self.reply(200,'{}')
        except (ValueError,KeyError,TypeError): self.reply(400,'{}')

if __name__ == '__main__':
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    url=f'http://127.0.0.1:{server.server_port}'
    print('PTT2 downloader:',url,flush=True)
    webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: STOP.set()
