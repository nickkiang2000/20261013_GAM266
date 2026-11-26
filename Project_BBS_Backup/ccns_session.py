"""CCNS Telnet navigation and numbered article capture, isolated from PTT."""
from collections import Counter, deque
import codecs
import hashlib
import re
import select
import socket
import time

import pyte
from pyte import modes
from wcwidth import wcwidth
from PyPtt import screens as _registered_codecs
from board_session import BoardNavigationError
from local_state import save_reading_capture

HOST = 'ccns.cc'
PORT = 23
WIDTH, HEIGHT = 80, 24
AID = re.compile(r'文章代碼\s*[:：]\s*#([A-Za-z0-9_-]{8})(?=\s|$)')
FOOTER = re.compile(r'瀏覽\s*第\s*(\d+)\s*/\s*(\d+)\s*頁\s*[（(]\s*(\d+)%\s*[）)].*?目前顯示\s*[:：]\s*第\s*(\d+)\s*[~～〜]\s*(\d+)\s*行')
PALETTE = ('black','red','green','brown','blue','magenta','cyan','white')


class CCNSError(BoardNavigationError):
    """A known CCNS state could not be verified; messages contain no credentials."""


class CCNSQueryUnavailable(CCNSError):
    """A single query cannot establish article identity; safe to defer only
    after the original board list has been freshly verified."""


class ConnectionClosed(ConnectionError):
    pass


class TelnetWire:
    """Bounded incremental IAC parser: terminal type, 80x24 NAWS, binary and SGA."""
    def __init__(self, sock):
        self.socket = sock
        self.state = 'data'
        self.option = None
        self.sub = bytearray()
        self.accepted = set()

    def write(self, value):
        self.socket.sendall(value.replace(b'\xff', b'\xff\xff'))

    def feed(self, data):
        output = bytearray()
        for value in data:
            if self.state == 'data':
                if value == 255: self.state = 'iac'
                else: output.append(value)
            elif self.state == 'iac':
                if value == 255: output.append(value); self.state = 'data'
                elif value in (251,252,253,254): self.option=value; self.state='option'
                elif value == 250: self.sub.clear(); self.state='sub'
                else: self.state='data'
            elif self.state == 'option':
                verb = self.option
                accepted = (value in (0,3,24,31) if verb == 253 else value in (0,1,3))
                if verb in (251,253):
                    response = (251 if accepted else 252) if verb == 253 else (253 if accepted else 254)
                    key=(verb,value,response)
                    if key not in self.accepted:
                        self.socket.sendall(bytes((255,response,value)));self.accepted.add(key)
                        if verb==253 and value==31 and accepted:
                            self.socket.sendall(b'\xff\xfa\x1f\x00\x50\x00\x18\xff\xf0')
                self.state='data'
            elif self.state == 'sub':
                if value == 255: self.state='sub_iac'
                elif len(self.sub)<1024: self.sub.append(value)
                else: raise CCNSError('Telnet 協商資料過長')
            elif self.state == 'sub_iac':
                if value == 240:
                    if bytes(self.sub)==b'\x18\x01':
                        self.socket.sendall(b'\xff\xfa\x18\x00VT100\xff\xf0')
                    self.state='data'
                elif value == 255: self.sub.append(255);self.state='sub'
                else: raise CCNSError('Telnet 協商格式無法確認')
        return bytes(output)

    def close(self):
        self.socket.close()


class Terminal(pyte.Screen):
    def __init__(self):
        self.unknown = 0
        self.hyperlinks = 0
        self.replies = []
        super().__init__(WIDTH, HEIGHT)
        self.stream = pyte.Stream(self)
        self.decoder = codecs.getincrementaldecoder('big5uao')(errors='strict')
        self.pending = b''

    def draw(self, data):
        # Big5 symbols (arrows, boxes, stars) occupy two cells even when
        # Unicode classifies them as ambiguous-width. Keep that station width.
        for original in data:
            char = original.translate(self.g1_charset if self.charset else self.g0_charset)
            width = 2 if len(original.encode('big5uao', errors='replace')) == 2 else wcwidth(original)
            if width <= 0:
                continue
            if self.cursor.x == self.columns:
                if modes.DECAWM in self.mode:
                    self.carriage_return(); self.linefeed()
                else:
                    self.cursor.x -= width
            if modes.IRM in self.mode:
                self.insert_characters(width)
            line = self.buffer[self.cursor.y]
            line[self.cursor.x] = self.cursor.attrs._replace(data=char)
            if width == 2 and self.cursor.x+1 < self.columns:
                line[self.cursor.x+1] = self.cursor.attrs._replace(data='')
            self.cursor.x = min(self.columns, self.cursor.x+width)
            self.dirty.add(self.cursor.y)

    def write_process_input(self, data):
        self.replies.append(data.encode('ascii'))

    def debug(self, *args, **kwargs):
        self.unknown += 1

    def feed(self, chunk):
        # A BBS can insert SGR between the two bytes of a Big5 character.
        # Decode text incrementally, without feeding ANSI bytes into that codec.
        data=self.pending+chunk; self.pending=b''
        while data:
            if data[0]!=27:
                pos=data.find(b'\x1b');pos=len(data) if pos<0 else pos
                self.stream.feed(self.decoder.decode(data[:pos]));data=data[pos:];continue
            if len(data)<2: self.pending=data;break
            if data[1]==91:
                match=re.match(rb'\x1b\[[0-?]*[ -/]*[@-~]',data)
                if not match:
                    if len(data)>128: raise CCNSError('終端控制碼過長')
                    self.pending=data;break
                token=match[0]
            elif data[1]==93:
                match=re.search(rb'\x07|\x1b\\',data[2:])
                if not match:
                    if len(data)>8192: raise CCNSError('終端標題控制碼過長')
                    self.pending=data;break
                # Window titles and OSC hyperlinks must never become article text.
                stop=2+match.end()
                if data.startswith(b'\x1b]8;'):self.hyperlinks+=1
                data=data[stop:];continue
            elif data[1] in (40,41,35):
                if len(data)<3:self.pending=data;break
                token=data[:3]
            else:token=data[:2]
            self.stream.feed(token.decode('ascii',errors='strict'));data=data[len(token):]

    @property
    def ready(self):
        return not self.pending and not self.decoder.getstate()[0]

    def rows(self):
        rows=[]
        for y in range(HEIGHT):
            cells=[self.buffer[y][x] for x in range(WIDTH)]
            # Unpainted/default-background right padding is not article content.
            while cells and cells[-1].data==' ' and cells[-1].bg in ('default','black') and not cells[-1].reverse:
                cells.pop()
            runs=[]
            for cell in cells:
                if not cell.data:continue  # trailing half of a wide character
                fg,bg=cell.fg,cell.bg;bold=cell.bold
                if fg.startswith('bright'):fg=fg[6:];bold=True
                if cell.reverse:fg,bg=('black' if bg=='default' else bg),('white' if fg=='default' else fg)
                if fg not in (*PALETTE,'default') or bg not in (*PALETTE,'default'):
                    raise CCNSError('文章含尚未支援的延伸色彩')
                style=[f'f{7 if fg=="default" else PALETTE.index(fg)}']
                if bg!='default':style.append(f'b{PALETTE.index(bg)}')
                if bold:style.append('hl')
                if runs and runs[-1][1]==style:runs[-1][0]+=cell.data
                else:runs.append([cell.data,style])
            rows.append(runs)
        return rows

    @property
    def text(self):
        return '\n'.join(''.join(self.buffer[y][x].data for x in range(WIDTH)) for y in range(HEIGHT))


def is_main(text):
    return '主功能表' in text.split('\n')[0] and '(B)oards' in text and '佈告討論區' in text


def boards_selected(text):
    return is_main(text) and any(re.match(r'^\s*>[_\s]*\(B\)oards\s+【\s*佈告討論區\s*】',line)
                                 for line in text.splitlines())


def duplicate_login_prompt(text):
    compact=re.sub(r'\s+','',text)
    return ('偵測到多重登入' in compact and '刪除其他重複的login' in compact.lower()
            and re.search(r'[（(]y/n[）)]',compact,re.I) is not None)


def login_ready(text):
    return '請按任意鍵' in text or is_main(text) or duplicate_login_prompt(text)


def query_continue(text):
    # Q was issued only from a verified list. Accept the completion prompt,
    # not only an eight-character AID, so a notice cannot stall the batch.
    tail=''.join(text.splitlines()[-3:])
    return '請按任意鍵' in tail and re.search(r'[（(]Y/n[）)]',tail,re.I) is None


def query_diagnostic(text):
    # Do not retain login screens or whole lists. Only fixed classifications
    # and the article-code field are recorded for unsupported query formats.
    match=re.search(r'文章代碼\s*[:：]\s*#([^\s]+)',text)
    code=match[1] if match else ''
    return dict(query_panel='文章代碼及資訊查詢' in text,
                has_continue=query_continue(text),code_present=bool(match),
                code_length=len(code),
                observed_code=code if re.fullmatch(r'[A-Za-z0-9_?.-]{1,32}',code) else None,
                permission_notice='權限不足' in text or '沒有權限' in text,
                missing_notice=any(word in text for word in ('已被刪除','已刪除','檔案不存在','找不到文章')))


def is_list(text, board):
    rows=text.splitlines()
    return bool(rows and re.search(r'看板\s*《\s*'+re.escape(board)+r'\s*》',rows[0],re.I)
                and re.search(r'編號\s*日\s*期\s*作\s*者',text) and '文章選讀' in text)


def selected_row(text):
    rows=text.splitlines()
    for y,row in enumerate(rows[3:],3):
        match=re.match(r'^\s*>\s*(\d+)\s+(.*)',row)
        if match:return int(match[1]),match[2]
        star=re.match(r'^\s*>\s*[★☆*]\s+(.*)',row)
        if not star:continue
        # CCNS replaces the number with a star in-place. Require independent
        # numeric anchors on BOTH sides; never treat the star's date as index.
        above=below=None
        for n in range(y-1,2,-1):
            anchor=re.match(r'^\s*(\d+)\s+',rows[n])
            if anchor:above=int(anchor[1])+y-n;break
        for n in range(y+1,len(rows)-1):
            anchor=re.match(r'^\s*(\d+)\s+',rows[n])
            if anchor:below=int(anchor[1])-(n-y);break
        if above is not None and above==below:return above,star[1]
        raise CCNSError('星號列缺少一致的上下篇號佐證，未猜測文章編號')
    raise CCNSError('無法確認列表游標所指的文章編號')


def selected_sticky(text):
    return any(re.match(r'^\s*>\s*[★☆*]\s+',row) for row in text.splitlines()[3:])


def page_extent(text):
    match=FOOTER.search(text.split('\n')[-1])
    if not match:return None
    page,pages,percent,first,last=map(int,match.groups())
    reported_first=first
    # The old reader can display the first screen as 0~N. It is a start
    # offset, not an extra visible source line. Row count is checked below.
    if first==0 and page==1:first=1
    if not (1<=page<=pages and 0<=percent<=100 and 1<=first<=last):return None
    extent=dict(page=page,pages=pages,percent=percent,first=first,last=last)
    if reported_first==0:extent['reported_first']=0
    return extent


def visible_runs(runs):
    """Compare rendered cells; foreground/bold cannot paint an ASCII space."""
    result=[]
    for text,style in runs:
        background=next((token for token in style if token.startswith('b')), 'b0')
        for char in text:
            signature=(background,) if char==' ' else tuple(sorted(set(style)-{'b0'}))
            result.append((char,signature))
    return result


def local_article_identity(board,number,row):
    # Read flags and push counts are mutable. Date, author and list title identify the row.
    match=re.fullmatch(r'e(?:\d{2})?\s+(\d{1,2}/\s*\d{1,2})\s+(\S+)\s+(.*)',row)
    if not match:return None
    date,author,title=match.groups()
    date=re.sub(r'\s+','',date)
    title=re.sub(r'^(?:◇|◆|□|⇒|→|=>|Re|轉)\s+', '',title).rstrip()
    value='\n'.join((board.casefold(),str(number),date,author,title))
    return f'CCNS-E-{number}-'+hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]


class NumberedPages:
    def __init__(self):
        self.lines={}
        self.wraps=[]
        self.final=None
        self.frames=0

    def add(self, terminal):
        extent=page_extent(terminal.text)
        if not extent:raise CCNSError('無法辨識 CCNS 文章行號')
        physical=terminal.rows()[:-1]
        count=extent['last']-extent['first']+1
        if count>len(physical):raise CCNSError('來源行號超出可見正文，保留待查')
        # A bright white trailing backslash near the right edge is a
        # system continuation only when folding also fits the source range.
        logical=[];wrapped=[];pending=[]
        for y,runs in enumerate(physical):
            value=''.join(text for text,_ in runs)
            cells=len(value.encode('big5uao',errors='replace'))
            marker=bool(runs and runs[-1][0]=='\\' and 'f7' in runs[-1][1]
                        and 'hl' in runs[-1][1] and WIDTH-4<=cells-1<WIDTH)
            marker = marker and y+1<len(physical) and any(
                text.strip() or (text and any(token.startswith('b') and token!='b0' for token in style))
                for text,style in physical[y+1])
            part=[[text,list(style)] for text,style in (runs[:-1] if marker else runs)]
            for text,style in part:
                if pending and pending[-1][1]==style:pending[-1][0]+=text
                else:pending.append([text,style])
            if marker:
                wrapped.append(extent['first']+len(logical)-1)
                if y==len(physical)-1:raise CCNSError('折行延伸超出本頁，保留待查')
            else:
                logical.append(pending);pending=[]
        if len(logical)<count or any(''.join(value for value,_ in row).strip() for row in logical[count:]):
            raise CCNSError('來源行號與正文列數不符')
        physical=logical
        first=extent['first']
        if self.lines and first>max(self.lines):raise CCNSError('翻頁未保留重疊行，無法確認接縫')
        if not self.lines and first!=1:raise CCNSError('未從文章第一行開始讀取')
        for offset,runs in enumerate(physical[:count]):
            number=first+offset
            if number in self.lines and visible_runs(self.lines[number])!=visible_runs(runs):
                raise CCNSError('相同行號的文字或配色不同，保留待查')
            if number not in self.lines:self.lines[number]=runs
        self.wraps.extend(wrapped)
        self.frames+=1
        self.final=extent
        return extent

    def post(self, aid, index, board, *, identity_verified=False):
        e=self.final or {}
        if not (e.get('page')==e.get('pages') and e.get('percent')==100 and self.lines
                and len(self.lines)==e.get('last') and set(self.lines)==set(range(1,e['last']+1))):
            raise CCNSError('尚未核對完整正文與文末')
        lines=[''.join(value for value,_ in self.lines[n]) for n in range(1,len(self.lines)+1)]
        body='\n'.join(lines)
        first=re.fullmatch(r'\s*作者\s*[:：]?\s*(.*?)\s+(?:站內|看板)\s*[:：]?\s*(\S+)\s*',lines[0])
        title=re.fullmatch(r'\s*標題\s*[:：]?\s*(.*?)\s*',lines[1]) if len(lines)>1 else None
        date=re.fullmatch(r'\s*時間\s*[:：]?\s*(.*?)\s*',lines[2]) if len(lines)>2 else None
        header_board=first[2] if first else None
        if not first and identity_verified:
            # Some older CCNS headers omit the board column entirely.
            from article_metadata import parse_date
            author_only=re.fullmatch(r'\s*作者\s*[:：]?\s*([A-Za-z0-9_-]+\s*\(.*\))\s*',lines[0])
            if author_only and title and date and parse_date(date[1]):
                first=(None,author_only[1],None)
        if not first or (header_board and header_board.casefold()!=board.casefold() and not identity_verified) or not title or not date:
            raise CCNSError('文章首部的看板、作者、標題或時間無法核對')
        return dict(aid=aid,index=index,board=board,author=first[1],title=title[1],date=date[1],
                    full_content=body,pass_format_check=True,terminal_verified=True,
                    original_header_board=header_board,soft_wrap_lines=sorted(set(self.wraps)),
                    ansi_lines={str(n-1):runs for n,runs in self.lines.items()},
                    ansi_coverage=dict(matched=len(lines),total=len(lines)),
                    ansi_diagnostics=dict(reader='ccns_numbered_pages',reached_end=True,
                                          frames=self.frames,final_extent=e),comments=[])


class CCNSSession:
    def __init__(self, *, check_stop=lambda:None, event=lambda *a,**k:None, connector=None):
        self.check_stop=check_stop;self.event=event;self.connector=connector or socket.create_connection
        self.terminal=Terminal();self.wire=None;self.connect_core=self
        self.operations=Counter();self.view=None;self.board=None;self.selected=None
        self.capture_mode='page';self.retry_height=None;self.notice=None
        self.cursor='>';self.last_raw=b'';self.samples=deque(maxlen=3);self.last_send=0

    def close(self):
        if self.wire:self.wire.close()
        self.wire=None

    def get_screen_queue(self):return [self.terminal.text]

    def _wait(self, predicate, *, timeout=12, fresh=False):
        end=time.monotonic()+timeout;last=None;received=not fresh;raw=bytearray()
        while time.monotonic()<end:
            self.check_stop()
            readable,_,_=select.select([self.wire.socket],[],[],.1)
            if readable:
                chunk=self.wire.socket.recv(65536)
                if not chunk:raise ConnectionClosed('CCNS transport closed')
                cooked=self.wire.feed(chunk)
                if cooked:
                    if len(raw)+len(cooked)>2_000_000:raise CCNSError('單一畫面回應過長')
                    raw.extend(cooked)
                    try:
                        self.terminal.feed(cooked)
                    except UnicodeError as exc:
                        self.event('ccns_terminal_error',view=self.view,error='encoding_or_control')
                        raise CCNSError('終端文字編碼或控制碼無法解析') from exc
                    received=True;last=time.monotonic()
                while self.terminal.replies:self.wire.write(self.terminal.replies.pop(0))
            if received and self.terminal.ready and predicate(self.terminal.text) and (last is None or time.monotonic()-last>=.35):
                self.last_raw=bytes(raw);return self.terminal.text
        self.event('ccns_wait_timeout', view=self.view,
                   article_extent=page_extent(self.terminal.text),
                   has_continue='請按任意鍵' in self.terminal.text,
                   main_menu=is_main(self.terminal.text),
                   board_list=bool(self.board and is_list(self.terminal.text,self.board)),
                   unknown_controls=self.terminal.unknown,
                   decoder_ready=self.terminal.ready)
        raise CCNSError('等待 CCNS 畫面逾時，已停止送鍵')

    def _command(self, keys, predicate, *, timeout=12):
        self.check_stop()
        remaining=.2-(time.monotonic()-self.last_send)
        if remaining>0:time.sleep(remaining)
        self.check_stop();self.wire.write(keys.encode('big5uao'));self.last_send=time.monotonic()
        self.operations['commands']+=1
        return self._wait(predicate,timeout=timeout,fresh=True)

    def login(self, user, password, **ignored):
        # Credentials go only to the server; neither screens nor raw login data
        # are written to the article diagnostics or the job log.
        if any(c in user+password for c in '\r\n\x1b\x00'):raise CCNSError('帳密含控制字元')
        sock=self.connector((HOST,PORT),timeout=12);sock.settimeout(1)
        self.wire=TelnetWire(sock)
        def prompt(text):
            return bool(re.search(r'(?:請輸入|輸入您的|您的|使用者).*?(?:代號|帳號)|(?:login|username|user id)\s*[:：]',text,re.I))
        self._wait(prompt)
        self._command(user+'\r',lambda s:bool(re.search(r'密碼|password',s,re.I)))
        text=self._command(password+'\r',login_ready)
        announcements=0
        duplicate_answered=False
        for step in range(16):
            if is_main(text):
                self.view='main';self.event('ccns_login_ready',announcements=announcements);return
            if duplicate_login_prompt(text):
                if duplicate_answered:raise CCNSError('重複登入提示未解除，已停止送鍵')
                duplicate_answered=True
                self.event('ccns_login_prompt',kind='duplicate_login',answer='keep_other_sessions')
                # Enter alone defaults to Y at this prompt. Explicit N keeps
                # existing PCMan/browser sessions; never disconnect them.
                text=self._command('n\r',login_ready)
                continue
            if '請按任意鍵' not in text:raise CCNSError('登入遇到尚未支援的帳號或連線確認提示')
            text=self._command(' ',login_ready)
            announcements+=1
        raise CCNSError('登入公告超過上限，已停止送鍵')

    def _enter(self, board):
        if self.board:
            if self.board!=board:raise CCNSError('禁止在同一批次切換看板')
            self._return_to_list();return
        if self.view!='main' or not is_main(self.terminal.text):raise CCNSError('尚未確認主功能表')
        # The user verified b selects Boards and Enter opens it.
        if not boards_selected(self.terminal.text):
            self._command('b',boards_selected)
        self.event('ccns_navigation',step='boards_selected')
        self._command('\r',lambda s:not is_main(s))
        self.event('ccns_navigation',step='boards_opened')
        self._command('s',lambda s:bool(re.search(r'看板|板名|版名|搜尋|名稱',s)))
        self.event('ccns_navigation',step='board_search_prompt')
        self._command(board+'\r',lambda s:is_list(s,board))
        self.board=board;self.view='list'
        self.event('ccns_board_ready',board=board)

    def confirm_list(self, board):
        return self.view=='list' and self.board==board and is_list(self.terminal.text,board)

    def _return_to_list(self):
        if self.view in ('query','query_notice','query_pending'):
            if not query_continue(self.terminal.text):raise CCNSError('文章查詢畫面未出現可確認的離開提示')
            self._command(' ',lambda s:is_list(s,self.board));self.view='list'
        elif self.view=='article':self._command('\x1b[D',lambda s:is_list(s,self.board));self.view='list'
        if not self.confirm_list(self.board):raise CCNSError('無法確認已返回原看板列表')

    def recover_list_after_read_error(self, board):
        if self.board!=board:return False
        try:
            if is_list(self.terminal.text,board):
                # Verify a fresh reply instead of trusting a stale snapshot.
                self._command('\x0c',lambda text:is_list(text,board));self.view='list'
            else:
                self._return_to_list()
            return self.confirm_list(board)
        except CCNSError:return False

    def _list_command(self, keys, board):
        # End, PageDown at the bottom, and selecting the current article can
        # legitimately produce no bytes. Ctrl+L explicitly redraws the list,
        # so each positioning command still requires a fresh verified reply.
        if not self.confirm_list(board):raise CCNSError('列表定位前無法確認原看板')
        return self._command(keys+'\x0c',lambda text:is_list(text,board))

    def get_newest_index(self, unused=None, *, board):
        self._enter(board)
        # End is the conventional board-list last-row command; the result is
        # verified from the selected numeric row, never inferred from a date.
        self._list_command('\x1b[4~',board)
        number,_=selected_row(self.terminal.text)
        visible=[int(m[1]) for line in self.terminal.text.splitlines()[3:]
                 if (m:=re.match(r'^\s*>?\s*(\d+)\s+',line))]
        if not visible or number!=max(visible):
            raise CCNSError('End 後未位於列表最後一篇，無法確認最新編號')
        final=self._list_command('\x1b[6~',board)
        if selected_row(final)[0]!=number:
            raise CCNSError('列表仍可向後翻頁，無法確認最新編號')
        self.operations['newest_queries']+=1
        return number

    def get_post(self, board, *, index=None, aid=None, query=False, **ignored):
        self._enter(board)
        if index is not None:keys=str(index)+'\r'
        elif aid and (local:=re.fullmatch(r'CCNS-E-(\d+)-[a-f0-9]{16}',aid)):
            index=int(local[1]);keys=str(index)+'\r'
        elif aid and re.fullmatch(r'[A-Za-z0-9_-]{8}',aid):keys='#'+aid+'\r'
        else:raise CCNSError('文章定位參數無效')
        self._list_command(keys,board)
        number,row=selected_row(self.terminal.text)
        sticky=selected_sticky(self.terminal.text)
        if index is not None and number!=index:raise CCNSError('列表篇號與要求不符')
        self.event('ccns_navigation',step='article_query',index=number)
        self.view='query_pending'
        text=self._command('q',query_continue)
        if not AID.search(text):
            diagnostic=query_diagnostic(text)
            # Only the specific e-marked lock notice permits reading without a server AID.
            local_identity=(local_article_identity(board,number,row)
                            and '文章代碼及資訊查詢' in text
                            and '文章被鎖定、編輯或者刪除中' in text
                            and not diagnostic['permission_notice'])
            if local_identity:
                found=local_article_identity(board,number,row)
                self.view='query_notice';self._return_to_list()
                returned_number,returned_row=selected_row(self.terminal.text)
                if returned_number!=number or local_article_identity(board,number,returned_row)!=found:
                    raise CCNSError('無 AID 文章返回列表後識別資料已變更')
                if aid and found!=aid:raise CCNSError('無 AID 文章的列表識別資料已變更')
                self.selected=dict(aid=found,index=number,is_lock=False,ccns_sticky=sticky,
                                   identity_kind='ccns_e_without_aid',source_aid=None)
                self.operations['post_queries']+=1
                return dict(self.selected)
            self.view='query_notice'
            self.event('ccns_query_unavailable',index=number,**diagnostic)
            self._return_to_list()
            reason=('站方拒絕文章代碼查詢：您的權限不足；未讀取或存檔'
                    if diagnostic['permission_notice'] else
                    '文章資訊查詢未取得可核對的 8 碼代碼，已回到原看板列表，保留待查')
            error=CCNSQueryUnavailable(reason)
            error.capture_diagnostics=dict(reader='ccns_query',reason=reason,**diagnostic)
            raise error
        found=AID.search(text)[1]
        self.view='query'
        if aid and found!=aid:raise CCNSError('文章代碼與要求不符')
        self.selected=dict(aid=found,index=number,is_lock=False,ccns_sticky=sticky)
        self.operations['post_queries']+=1
        self._return_to_list()
        return dict(self.selected)

    def read_colored_post(self, board, aid, *, _verify_local=True):
        current=self.get_post(board,aid=aid,query=True)
        collector=NumberedPages();self.samples.clear();unknown=self.terminal.unknown;hyperlinks=self.terminal.hyperlinks
        self.view='article'
        first_sample=None
        try:
            self._command('\x1b[C',lambda s:page_extent(s) is not None)
            for step in range(5000):
                self.check_stop()
                if page_extent(self.terminal.text):
                    self.samples.append(dict(raw=self.last_raw,screen='\n'.join(''.join(v for v,_ in r) for r in self.terminal.rows()),
                                             colors=self.terminal.rows(),height=HEIGHT,key='open' if step==0 else 'PageDown'))
                if first_sample is None and self.samples:first_sample=self.samples[0]
                if self.terminal.unknown!=unknown:raise CCNSError('正文含未支援的終端控制碼')
                if self.terminal.hyperlinks!=hyperlinks:raise CCNSError('正文含隱藏超連結目標，保留待查避免截短網址')
                previous=collector.final
                extent=collector.add(self.terminal)
                # 99% -> 100% can change the page/first line while keeping
                # the same final line. Full continuity is checked by post().
                if extent['page']==extent['pages'] and extent['percent']==100:break
                if previous and extent['last']<=previous['last']:raise CCNSError('翻頁後行號未前進')
                self._command('\x1b[6~',lambda s:page_extent(s) is not None)
                self.operations['article_pages']+=1
            else:raise CCNSError('文章超過首版讀取頁數上限')
            self._return_to_list()
            verified=self.get_post(board,index=current['index'],query=True)
            if verified['aid']!=aid:raise CCNSError('讀完後文章代碼已變更，未存檔')
            post=collector.post(aid,current['index'],board,identity_verified=True)
            post['ccns_sticky']=bool(verified.get('ccns_sticky'))
            if current.get('identity_kind')=='ccns_e_without_aid':
                post.update(identity_kind='ccns_e_without_aid',source_aid=None,local_identity=aid)
                if _verify_local:
                    repeat=self.read_colored_post(board,aid,_verify_local=False)
                    if (post['full_content']!=repeat['full_content'] or
                            {n:visible_runs(r) for n,r in post['ansi_lines'].items()}!=
                            {n:visible_runs(r) for n,r in repeat['ansi_lines'].items()}):
                        raise CCNSError('無 AID 文章兩次讀取的正文或配色不一致，保留待查')
                    post['ansi_diagnostics']['identity_verification']='same_list_identity_and_two_complete_reads'
            return post
        except CCNSError as exc:
            diagnostic=dict(reader='ccns_numbered_pages',frames=collector.frames,
                            final_extent=collector.final,reason=str(exc))
            if self.samples:
                samples=list(self.samples)
                if first_sample is not None and not any(sample is first_sample for sample in samples):
                    samples.insert(0,first_sample)
                path=save_reading_capture(board,aid,'ccns_page',samples,diagnostic,str(exc))
                if path:diagnostic['capture_file']=str(path)
            exc.capture_diagnostics=diagnostic
            raise
