"""Import a locally saved ANSI article into an existing board backup."""

import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from ansi_capture import ColorScreen, SGR


def parse_ansi_article(data):
    try:
        text = data.decode('utf-8-sig')
        encoding = 'UTF-8'
    except UnicodeDecodeError:
        text = data.decode('cp950', errors='replace')
        encoding = 'CP950'
    plain = SGR.sub('', text)
    if any(ord(char) < 32 and char not in '\r\n\t' for char in plain):
        raise ValueError('此 ANSI 檔含有 SGR 配色以外的終端控制碼')
    header = plain.splitlines()
    if len(header) < 2:
        raise ValueError('ANSI 檔缺少文章標頭')
    author = re.match(r'^\s*作者\s*[:：]?\s*(\S.*)', header[0])
    title = re.match(r'^\s*標題\s*[:：]?\s*(\S.*)', header[1])
    if not author or not title:
        raise ValueError('無法從 ANSI 檔開頭確認作者和標題')
    author_text = author[1].strip()
    board = re.search(r'\s+看板\s*[:：]?\s*([A-Za-z0-9_-]+)\s*$', author_text)

    palette = ColorScreen(24)
    lines = []
    colors = {}
    for index, line in enumerate(text.splitlines()):
        runs = []
        start = 0
        for match in SGR.finditer(line):
            if match.start() > start:
                runs.append((line[start:match.start()], palette.classes))
            palette.sgr(match[1])
            start = match.end()
        if start < len(line):
            runs.append((line[start:], palette.classes))
        lines.append(''.join(value for value, _ in runs))
        if any(style for _, style in runs):
            colors[str(index)] = runs
    post = {'author': author_text[:board.start()].rstrip() if board else author_text,
            'title': title[1].strip(), 'full_content': '\n'.join(lines),
            'ansi_lines': colors, 'pass_format_check': True, 'terminal_verified': True,
            'manual_ansi': {'encoding': encoding, 'decode_replacements': text.count('\ufffd')}}
    return board[1] if board else None, post


def check_ansi_target(db, site, board, index, aid, original):
    if index < 1 or not re.fullmatch(r'[A-Za-z0-9_-]{8}', aid):
        raise ValueError('請輸入有效的文章編號與 AID')
    issue = db.execute('SELECT 1 FROM article_issues WHERE site=? AND board=? AND idx=? AND aid=?',
                       (site, board, index, aid)).fetchone()
    existing = db.execute('SELECT data FROM articles WHERE site=? AND board=? AND aid=?',
                          (site, board, aid)).fetchone()
    same_source = bool(existing and (json.loads(existing[0]).get('manual_ansi') or {}).get('original') == original)
    color = db.execute('SELECT 1 FROM color_line_checks WHERE site=? AND board=? AND idx=? AND aid=?', (site,board,index,aid)).fetchone()
    if not issue and not color and not same_source:
        raise ValueError('這份備份沒有同編號與 AID 的待查紀錄')
    if existing and not same_source and not (issue or color):
        raise ValueError('此 AID 已有另一份正文')
    if db.execute('SELECT 1 FROM articles WHERE site=? AND board=? AND source_index=? AND aid<>?',
                  (site, board, index, aid)).fetchone():
        raise ValueError('這個文章編號已有不同的 AID')
    return same_source


def prepare_ansi_article(data, board, *, site, index, aid, filename=''):
    file_board, post = parse_ansi_article(data)
    if file_board and file_board.casefold() != board.casefold():
        raise ValueError('ANSI 檔的看板與指定看板不同')
    match = re.fullmatch(r'(PTT2?)_([A-Za-z0-9_-]+)_(\d+)_([A-Za-z0-9_-]{8})\.ans', filename, re.I)
    if match and (match[1].upper(), match[2].casefold(), int(match[3]), match[4]) != (
            site, board.casefold(), index, aid):
        raise ValueError('ANSI 檔名的站台、看板、篇號或 AID 與所選紀錄不同')
    if not file_board and not match:
        raise ValueError(f'ANSI 標頭缺少看板。請核對來源，將檔名命名為 {site}_{board}_{index}_{aid}.ans 後再預覽')
    post['manual_ansi']['identity_from_filename'] = not file_board
    post['manual_ansi']['original'] = f'manual-ansi/{sha256(data).hexdigest()}.ans'
    return post


def store_ansi_article(db, data, post, folder, site, board, index, aid, *, save_article):
    original_name = post['manual_ansi']['original']
    original = folder / original_name
    post['aid'] = aid
    with db:
        db.execute('BEGIN IMMEDIATE')
        same_source = check_ansi_target(db, site, board, index, aid, original_name)
        original.parent.mkdir(exist_ok=True)
        if not original.exists():
            with NamedTemporaryFile(dir=original.parent, delete=False) as copy:
                temp = Path(copy.name)
                copy.write(data)
            try:
                temp.replace(original)
            finally:
                temp.unlink(missing_ok=True)
        if not same_source:
            previous = db.execute('SELECT data FROM articles WHERE site=? AND board=? AND aid=?',(site,board,aid)).fetchone()
            if previous:
                from app import preserve_article_version
                preserve_article_version(db,site,board,aid,json.loads(previous[0]),'previous')
            save_article(db, board, post, site=site, source_index=index, commit=False)
            db.execute('DELETE FROM color_line_checks WHERE site=? AND board=? AND aid=?',(site,board,aid))
    return original


def import_ansi_article(source, folder, site, board, index, aid):
    import app

    if not (folder / 'articles.sqlite3').is_file():
        raise ValueError('找不到既有的備份資料庫')
    data = source.read_bytes()
    post = prepare_ansi_article(data, board, site=site, index=index, aid=aid, filename=source.name)
    with closing(app.database(folder)) as db:
        original = store_ansi_article(db, data, post, folder, site, board, index, aid,
                                      save_article=app.save_article)
        app.publish_archive(db, folder)
    return original


if __name__ == '__main__':
    import app

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('folder', type=Path)
    parser.add_argument('site', choices=app.SITES)
    parser.add_argument('board')
    parser.add_argument('index', type=int)
    parser.add_argument('aid')
    args = parser.parse_args()
    original = import_ansi_article(args.source, args.folder, args.site, args.board, args.index, args.aid)
    print(original)
