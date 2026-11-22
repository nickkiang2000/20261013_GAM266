"""List metadata derived without rewriting article text or guessing a posting year."""
from datetime import datetime, timezone, timedelta
from html import escape
import re
from urllib.parse import urlsplit

TAIPEI = timezone(timedelta(hours=8))
AID_TABLE = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_'
MONTHS = {name: i for i, name in enumerate('Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(), 1)}


def author_id(author):
    return str(author or '').split('(', 1)[0].strip()


def parse_date(value):
    text = str(value or '').strip()
    match = re.fullmatch(r'(?:[A-Za-z]{3}\s+)?([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{4})', text)
    try:
        if match:
            month, day, hour, minute, second, year = match.groups()
            return datetime(int(year), MONTHS[month.title()], int(day), int(hour), int(minute), int(second), tzinfo=TAIPEI)
        match = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{2}):(\d{2}):(\d{2})(?:\s+[A-Za-z]{3})?)?', text)
        if match:
            month, day, year, hour, minute, second = match.groups()
            return datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0), tzinfo=TAIPEI)
        # ISO dates include an explicit year; naive timestamps are station time.
        value = datetime.fromisoformat(text.replace('/', '-'))
        return value.replace(tzinfo=TAIPEI) if value.tzinfo is None else value.astimezone(TAIPEI)
    except (ValueError, KeyError):
        return None


def list_month_day(value):
    match = re.fullmatch(r'\s*(\d{1,2})/(\d{1,2})\s*', str(value or ''))
    if not match: return None
    month, day = map(int, match.groups())
    try: datetime(2000, month, day)
    except ValueError: return None
    return month, day


def posting_date(post, body='', *, site='', board='', aid=''):
    """Return a display date and its evidence. Unknown years remain explicitly unknown."""
    def result(value, source):
        return value.strftime('%Y/%m/%d'), source
    value = parse_date(post.get('date'))
    if value: return result(value, '文章時間欄')
    # Only the first header, with the outer author AND board and title verified.
    # A forwarded header deeper in the body is never used as the posting date.
    rows = str(body or '').lstrip('\ufeff\r\n').splitlines()
    if len(rows) >= 3:
        author = re.fullmatch(r'作者\s*[:：]\s*(.*?)\s+看板\s*[:：]\s*(\S+)\s*', rows[0])
        title = re.fullmatch(r'標題\s*[:：]\s*(.*?)\s*', rows[1])
        date = re.fullmatch(r'時間\s*[:：]\s*(.*?)\s*', rows[2])
        if (author and title and date and board and post.get('author') and post.get('title')
                and author.group(2).casefold() == board.casefold()
                and author_id(author.group(1)) == author_id(post['author'])
                and title.group(1) == str(post['title']).strip()):
            value = parse_date(date.group(1))
            if value: return result(value, '正文首部時間（已核對作者、看板與標題）')
    month_day = list_month_day(post.get('list_date'))
    if site in ('PTT', 'PTT2') and re.fullmatch(r'[0-9A-Za-z_-]{8}', aid):
        # Inverse of the locally installed PyPtt.lib_util.get_aid_from_url.
        stamp = 0
        for char in aid[:6]: stamp = stamp * 64 + AID_TABLE.index(char)
        try: value = datetime.fromtimestamp(stamp, TAIPEI)
        except (OverflowError, OSError, ValueError): value = None
        if value and 1990 <= value.year <= datetime.now(TAIPEI).year:
            try: url = urlsplit(str(post.get('url') or ''))
            except ValueError: url = urlsplit('')
            host = 'www.ptt.cc' if site == 'PTT' else 'www.ptt2.cc'
            path = re.fullmatch(r'/bbs/' + re.escape(board) + r'/M\.(\d{9,11})\.A(?:\.[0-9A-Fa-f]+)?\.html', url.path)
            url_matches = (url.scheme in ('http', 'https') and url.hostname == host and path and int(path.group(1)) == stamp)
            agrees = month_day == (value.month, value.day)
            if (url_matches and (month_day is None or agrees)) or agrees:
                return result(value, '文章識別碼時間戳＋' + ('列表月／日核對' if agrees else '站方文章網址核對'))
    if month_day:
        return f'{month_day[0]:02d}/{month_day[1]:02d}（年份待查）', '僅有列表月／日，未推測年份'
    return '日期待查', '尚無可核對的發文日期'


def push_cell(post):
    raw = post.get('push_number')
    value = str(raw).strip() if raw is not None else ''
    if not re.fullmatch(r'(?:[0-9]{1,3}|爆|X[1-9]|XX)', value): value = ''
    kind = ('boom' if value == '爆' else 'negative' if value.startswith('X')
            else 'hot' if value.isdigit() and int(value) >= 10 else 'positive')
    source = ('站方列表推文數／評價標記（備份時）' if value else
              '站方列表未顯示數值' if 'push_number' in post else '未保存站方推文數')
    return f'<span class="entry-push push-{kind}" title="{source}">{escape(value)}</span>'


def list_cells(index, post, title, body='', *, site='', board='', aid='', sticky=False):
    date, source = posting_date(post, body, site=site, board=board, aid=aid)
    number = '<span class="entry-number sticky-star">★</span>' if sticky else '<span class="entry-number">' + escape(str(index) if index is not None else '—') + '</span>'
    manual = '<small class="manual-label">（人工備份）</small>' if post.get('manual_ansi') else ''
    marker = '' if not post or re.match(r'^\s*(?:re\s*:|r:)', str(title), re.I) else '<span class="topic-mark">□</span> '
    return ('<span class="entry-meta">' + number + push_cell(post) + '<span class="entry-date" title="' + escape(source, quote=True) + '">' + escape(date) + '</span>'
            + '<span class="entry-author" title="' + escape(str(post.get('author') or '—'), quote=True) + '">' + escape(str(post.get('author') or '—')) + '</span>'
            + '</span><span class="entry-title">' + marker + escape(str(title)) + manual + '</span>')
