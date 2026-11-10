"""Read a board in one terminal session using the pinned PyPtt parser."""

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import re
import time

from PyPtt import _api_get_newest_index, _api_get_post, _api_util
from PyPtt import command, connect_core, data_type, exceptions, lib_util, screens
from PyPtt._api_get_post import AutoJsonEncoder, PostField
from websockets.exceptions import ConnectionClosed as SocketClosed


MIN_COMMAND_INTERVAL = 1.0
TAIPEI = timezone(timedelta(hours=8))


class BoardNavigationError(RuntimeError):
    """The terminal did not reach a state in which it is safe to keep sending keys."""


class StationRateLimit(RuntimeError):
    def __init__(self, details):
        super().__init__('站方限制切換看板、搜尋或讀取文章')
        self.details = details


class BoardTemporarilyClosed(RuntimeError):
    """The station explicitly reported a temporary board closure."""


class ArticleStalled(RuntimeError):
    def __init__(self, line):
        super().__init__(f'文章翻頁停在第 {line} 行，已回到看板列表')
        self.line = line


def is_temporary_closure(screen):
    rows = [row.strip() for row in screen.splitlines() if row.strip()]
    if not rows:
        return False
    candidates = rows[:4] if rows[-1] in ('請按任意鍵繼續', '任意鍵繼續') else rows[:1]
    return any(re.match(r'^[★※＊!！]*\s*(?:本|此|該)?(?:看板|版面)(?:目前)?(?:已)?暫時關閉', row)
               for row in candidates)


def detect_rate_limit(screen, now=None):
    header = next((line for line in screen.splitlines() if line.strip()), '')
    if not re.match(r'^\s*使用限制\s+限速中', header) and not re.search(
            r'^\s*[★※＊!！]*\s*程式耗用過多', header):
        return None
    now = now or datetime.now(TAIPEI)
    match = re.search(r'預計\s*(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*恢復', screen)
    resume_at = None
    if match:
        month, day, hour, minute = map(int, match.groups())
        try:
            resume_at = datetime(now.year, month, day, hour, minute, tzinfo=TAIPEI)
            if month == 1 and now.month == 12:
                resume_at = resume_at.replace(year=now.year + 1)
        except ValueError:
            pass
    counts = re.search(r'進入看板\s*([\d,]+)\s*次\s*[、,，]\s*搜尋\s*([\d,]+)\s*次', screen)
    return {
        'until': (resume_at + timedelta(minutes=1) if resume_at and resume_at + timedelta(minutes=1) > now
                  else now + timedelta(hours=1)).isoformat(timespec='seconds'),
        'station_resume_at': resume_at.isoformat(timespec='minutes') if resume_at else None,
        'station_board_entries': int(counts[1].replace(',', '')) if counts else None,
        'station_searches': int(counts[2].replace(',', '')) if counts else None,
    }


class BoardSession:
    """One entry into the board, then read only from verified list/article states."""

    def __init__(self, api):
        self.api = api
        self.connect_core = api.connect_core
        self.board = None
        self.view = None
        self.selected = None
        self.index_mode = False
        self.operations = Counter()
        self.notice = None
        self.notice_screen = ''
        self.last_sent = None
        self.pacing = False
        self.send = self.connect_core.send
        self.decode = self.connect_core._decode_screen
        self.connect_core.send = self._send
        self.connect_core._decode_screen = self._decode

    @property
    def cursor(self):
        return self.api.cursor

    def _decode(self, screen, *args, **kwargs):
        limit = detect_rate_limit(screen)
        notice = StationRateLimit(limit) if limit else BoardTemporarilyClosed() if is_temporary_closure(screen) else None
        if notice:
            self.notice = notice
            self.notice_screen = screen
            raise notice
        return self.decode(screen, *args, **kwargs)

    def _send(self, message, targets, *args, **kwargs):
        if self.notice:
            raise self.notice
        if self.pacing and message is None:
            self.operations['receive_only_waits'] += 1
        if self.pacing and message:
            if self.last_sent is not None:
                remaining = MIN_COMMAND_INTERVAL - (time.monotonic() - self.last_sent)
                if remaining > 0:
                    time.sleep(remaining)
            self.last_sent = time.monotonic()
            self.operations['terminal_requests'] += 1
            if message.startswith(command.go_main_menu + 'qs'):
                if self.operations['board_entries']:
                    self.operations['board_entry_retries'] += 1
                self.operations.update(('board_entries', 'board_searches'))
            elif message == command.enter * 2:
                self.operations['article_opens'] += 1
            elif any(t.get_detect_target() == screens.Target.PostEnd for t in targets):
                self.operations['article_steps'] += 1
        return self.send(message, targets, *args, **kwargs)

    def _screen(self):
        queue = self.connect_core.get_screen_queue()
        return queue[-1] if queue else ''

    def _list_targets(self):
        return [connect_core.TargetUnit(screens.Target.InBoard, break_detect=True)]

    def _checked_send(self, keys, targets, **kwargs):
        result = self.connect_core.send(keys, targets, **kwargs)
        if result < 0:
            if self.connect_core.last_timeout_was_silent:
                raise exceptions.ConnectionClosed()
            raise BoardNavigationError()
        return result

    def _require_list(self):
        if not _api_util.is_article_list(self.api, self._screen()):
            raise BoardNavigationError()
        self.view = 'list'
        self.selected = None

    def _enter(self, board):
        if self.board is not None:
            if board != self.board:
                raise BoardNavigationError()
            return
        self.pacing = True
        _api_util.goto_board(self.api, board)
        if not _api_util.is_article_list(self.api, self._screen()):
            # The SDK may recognize the board header before the list redraw finishes.
            self._checked_send(None, self._list_targets())
        self._require_list()
        self.board = board

    def _return_to_list(self):
        if self.view == 'query':
            self._checked_send(command.enter, self._list_targets())
        elif self.view == 'article':
            self._checked_send(command.left, self._list_targets())
        self._require_list()

    def recover_notice(self, board, until, stop):
        """Wait on the existing socket, then acknowledge one recognized prompt."""
        async def wait():
            screen = self.notice_screen
            while not stop.is_set():
                remaining = (until - datetime.now(TAIPEI)).total_seconds()
                # Read briefly even at the deadline to catch any queued state change.
                try:
                    chunk = await asyncio.wait_for(self.connect_core._recv(), min(1, max(0.1, remaining)))
                except TimeoutError:
                    if remaining <= 0:
                        return screen
                except SocketClosed as exc:
                    raise exceptions.ConnectionClosed() from exc
                else:
                    screen = self.connect_core._stream_screen(self.connect_core.current_encoding, chunk)
                    if remaining <= 0:
                        return screen
            return None

        screen = self.connect_core._get_event_loop().run_until_complete(wait())
        if screen is None or stop.is_set():
            return False
        limit = detect_rate_limit(screen)
        if (limit and limit['station_resume_at']
                and datetime.fromisoformat(limit['station_resume_at']) + timedelta(minutes=1) > until):
            raise StationRateLimit(limit)
        self.notice = None
        menu = connect_core.TargetUnit(screens.Target.MainMenu, break_detect=True)
        if limit or is_temporary_closure(screen):
            if not any(row.strip() in ('請按任意鍵繼續', '任意鍵繼續') for row in screen.splitlines()):
                raise BoardNavigationError()
            self.operations['notice_acknowledgements'] += 1
            self._checked_send(command.enter, self._list_targets() + [menu], refresh=False)
            screen = self._screen()
        elif _api_util.is_article_list(self.api, screen):
            self._checked_send(command.refresh, self._list_targets())
            screen = self._screen()
        if menu.is_match(screen, self.cursor):
            self.board = None
            self._enter(board)
        else:
            if not _api_util.is_article_list(self.api, screen):
                raise BoardNavigationError()
            self._require_list()
            self.board = board
        return True

    def _recover_stalled_article(self, exc):
        match = re.fullmatch(r'get_content: stuck at line (\d+)', str(exc))
        if not match:
            return
        screen = self._screen()
        if _api_util.is_article_list(self.api, screen):
            self._require_list()
        elif any(connect_core.TargetUnit(target).is_match(screen, self.cursor)
                 for target in (screens.Target.InPost, screens.Target.PostEnd)):
            self.view = 'article'
            self._return_to_list()
        else:
            return
        self.operations['stalled_articles'] += 1
        raise ArticleStalled(int(match[1])) from exc

    def _cursor_row(self):
        for line in self._screen().splitlines():
            if line.startswith(self.cursor):
                match = re.match(r'\s*(\d+)\s', line[len(self.cursor):])
                if match:
                    return int(match[1]), line
        return None, ''

    def _position(self, index):
        current, row = self._cursor_row()
        if current == index:
            return row
        if current is not None and abs(current - index) == 1:
            self.operations['cursor_moves'] += 1
            self._checked_send(command.up if index < current else command.down, self._list_targets())
            current, row = self._cursor_row()
            if current == index:
                return row
        self.operations['number_jumps'] += 1
        self._checked_send(str(index) + command.enter, self._list_targets())
        current, row = self._cursor_row()
        if current != index:
            raise BoardNavigationError()
        return row

    def get_newest_index(self, index_type, *, board):
        self._enter(board)
        self.operations['newest_checks'] += 1
        targets = self._list_targets() + [connect_core.TargetUnit('沒有文章...', break_detect=True)]
        if self._checked_send('$', targets) == 1:
            return 0
        self._require_list()
        return _api_get_newest_index._get_newest_index(self.api)

    def _query(self, keys):
        self.operations['post_queries'] += 1
        target = screens.Target.PTT1_QueryPost if self.api.config.host == data_type.HOST.PTT1 else screens.Target.PTT2_QueryPost
        self._checked_send(keys + command.query_post, [connect_core.TargetUnit(target, break_detect=True)])
        self.view = 'query'
        self.selected = _api_util.parse_query_post(self.api, self._screen())
        return self.selected

    def get_post(self, board, *, index=None, aid=None, query=False):
        self._enter(board)
        if index is not None and query:
            self.index_mode = True
            self._return_to_list()
            row = self._position(index)
            if re.search(r'\d{1,2}/\d{1,2}\s+-\s+□\s*[（(]本文已被(?:刪除|吃掉)[）)]', row):
                return {'index': index, 'post_status': 'DELETED_BY_UNKNOWN'}
            selected = self._query('')
            if selected.index != index:
                raise BoardNavigationError()
            return {'aid': selected.aid, 'index': selected.index, 'is_lock': selected.lock_post}

        if self.view == 'query' and self.selected.aid == aid:
            selected = self.selected
        else:
            if self.index_mode:
                raise BoardNavigationError()
            self._return_to_list()
            self.operations['aid_searches'] += 1
            selected = self._query(lib_util.check_aid(aid) + command.enter)
        if selected.aid != aid:
            return {'aid': selected.aid}
        post = {
            PostField.board: board, PostField.aid: aid, PostField.index: selected.index,
            PostField.author: selected.author, PostField.title: selected.title,
            PostField.url: selected.url, PostField.money: selected.money,
            PostField.list_date: selected.list_date, PostField.push_number: selected.push_number,
            PostField.is_lock: selected.lock_post, PostField.pass_format_check: selected.lock_post,
            PostField.post_status: data_type.PostStatus.EXISTS, PostField.comments: [],
        }
        if not selected.lock_post:
            try:
                body, has_control_code = _api_util.get_content(self.api)
            except exceptions.UnknownError as exc:
                self._recover_stalled_article(exc)
                raise
            if body is None:
                # The current screen may be a prompt, so leave navigation to a new session.
                raise BoardNavigationError()
            self.view = 'article'
            post.update({PostField.full_content: body, PostField.has_control_code: has_control_code,
                         PostField.is_unconfirmed: self.api.Unconfirmed})
            if len(body.split('\n', 3)) >= 3:
                post = _api_get_post._parse_post_content(self.api, post, board, body)
        return json.loads(json.dumps(post, cls=AutoJsonEncoder))
