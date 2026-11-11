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
END_KEY = '\x1b[4~'
FALLBACK_HEIGHT = 24
FALLBACK_MAX_STEPS = 128
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
    def __init__(self, line, reason=None):
        if reason is None:
            reason = f'文章翻頁停在第 {line} 行' if line else '無法確認文章已完整讀到文末'
        super().__init__(f'{reason}，已回到看板列表')
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
        self.retry_height = None
        self.last_sent = None
        self.pacing = False
        self.capture_mode = 'line'
        self.check_stop = None
        self.send = self.connect_core.send
        self.decode = self.connect_core._decode_screen
        self.connect_core.send = self._send
        self.connect_core._decode_screen = self._decode

    @property
    def cursor(self):
        return self.api.cursor

    def _decode(self, screen, *args, **kwargs):
        self._check_notice(screen)
        return self.decode(screen, *args, **kwargs)

    def _check_notice(self, screen):
        limit = detect_rate_limit(screen)
        notice = StationRateLimit(limit) if limit else BoardTemporarilyClosed() if is_temporary_closure(screen) else None
        if notice:
            self.notice = notice
            self.notice_screen = screen
            raise notice

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

    def _refresh_list(self):
        self._checked_send(command.refresh, self._list_targets())
        self._require_list()

    def _resize_list(self, height):
        self._require_list()
        core = self.connect_core
        recv = core._recv
        chunks = []

        async def capture_redraw():
            chunk = await recv()
            chunks.append(chunk)
            return chunk

        core._recv = capture_redraw
        try:
            core.set_screen_height(height)
        finally:
            core._recv = recv
        # PyPtt drains the resize redraw before returning. Verify that actual
        # response first, rather than depending on a second Ctrl-L redraw.
        if chunks and core._pending and core._pending[0] is chunks[-1]:
            chunks.pop()
        redraw = b''.join(chunks)
        clear = redraw.find(b'\x1b[2J')
        if clear >= 0:
            start = redraw.rfind(b'\x1b[?2026h', 0, clear + 1)
            if start < 0:
                start = clear
            end = redraw.find(b'\x1b[?2026l', clear)
            parser = screens.IncrementalScreen(core.current_encoding, height)
            parser.feed(redraw[start:end + len(b'\x1b[?2026l')] if end >= 0 else redraw[start:])
            screen = parser.screen
            self._check_notice(screen)
            if (start == clear or end >= 0) and _api_util.is_article_list(self.api, screen):
                core._RDQ.add(screen)
                self._require_list()
                self.operations['resize_redraws_verified'] += 1
                return
        self.operations['resize_refreshes'] += 1
        self._refresh_list()

    def _restore_height(self):
        if self.retry_height is not None:
            self._resize_list(self.retry_height)
            self.retry_height = None

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
        self._restore_height()
        return True

    def _article_targets(self):
        return [connect_core.TargetUnit(screens.Target.PostEnd, break_detect=True),
                connect_core.TargetUnit(screens.Target.InPost, break_detect=True),
                *self._list_targets()]

    def _send_article_key(self, key):
        targets = self._article_targets()
        if self.capture_mode == 'page' and key == command.enter * 2:
            targets.append(connect_core.TargetUnit(screens.Target.Animation, break_detect=True))
        result = self._checked_send(key, targets)
        if result == 2:
            self._require_list()
        elif result in (0, 1):
            self.view = 'article'
        return result

    def _verify_end(self, get_numbering):
        numbering = get_numbering()
        end = numbering.last_window
        if not end or not numbering.covers(end[1]) or numbering.color_conflicts:
            return None
        if self._send_article_key(command.up) == 2:
            return None
        earlier = numbering.last_window
        if not earlier or earlier[1] >= end[1]:
            return None
        self.operations['end_checks'] += 1
        if self._send_article_key(END_KEY) == 2:
            return None
        return numbering.confirm_end(end)

    def _read_numbered_article(self, get_numbering):
        result = self._send_article_key(command.enter * 2)
        numbering = get_numbering()
        for step in range(FALLBACK_MAX_STEPS + 1):
            if result == 0:
                return numbering.assemble() if not numbering.color_conflicts else None
            previous = numbering.last_window
            if (result == 2 or step == FALLBACK_MAX_STEPS or previous is None
                    or not numbering.covers(previous[1]) or numbering.color_conflicts):
                break
            result = self._send_article_key(command.down)
            current_window = numbering.last_window
            if result == 1 and current_window and current_window[1] <= previous[1]:
                return self._verify_end(get_numbering)
        return None

    def _read_article_pages(self, get_numbering):
        """Use one right key per validated screen and stop on the captured end screen."""
        key = command.enter * 2
        last = 0
        while True:
            if self.check_stop:
                self.check_stop()
            result = self._send_article_key(key)
            if result == 3:
                # Decline automatic playback so each screen advances on one key.
                key = 'n'
                continue
            numbered = get_numbering()
            window = numbered.last_window
            if result == 2:
                reason = '逐頁擷取在確認文末前回到看板'
            elif not window or numbered.width_mismatches or numbered.unnumbered:
                reason = '逐頁擷取無法核對畫面列與站方行號'
            elif window[0] > last + 1:
                reason = f'逐頁擷取缺少第 {last + 1}～{window[0] - 1} 行'
            elif numbered.conflicts:
                reason = '逐頁擷取的重疊行正文不一致'
            elif window[1] <= last:
                reason = '逐頁擷取未取得新的正文行'
            elif result == 0:
                body = numbered.assemble()
                if body is not None:
                    return body
                reason = '逐頁擷取已到文末，但未能核對完整正文'
            else:
                last = window[1]
                key = command.right
                continue
            self._return_to_list()
            raise ArticleStalled(window[1] if window else last or None, reason)

    def _read_at_fallback_height(self, selected, get_numbering):
        self.retry_height = self.connect_core.config.screen_height
        self._resize_list(FALLBACK_HEIGHT)
        self._position(selected.index)
        current = self._query('')
        body = None
        if current.aid == selected.aid and current.index == selected.index and not current.lock_post:
            body = self._read_numbered_article(get_numbering)
        self._return_to_list()
        self._restore_height()
        return body

    def _recover_stalled_article(self, exc, selected=None, get_numbering=None, fallback_attempts=None):
        match = re.fullmatch(r'get_content: stuck at line (\d+)', str(exc))
        if not match:
            return
        screen = self._screen()
        if _api_util.is_article_list(self.api, screen):
            self._require_list()
        elif any(connect_core.TargetUnit(target).is_match(screen, self.cursor)
                 for target in (screens.Target.InPost, screens.Target.PostEnd)):
            self.view = 'article'
            if get_numbering is not None:
                attempt = {'height': self.connect_core.config.screen_height, 'method': 'end_round_trip'}
                fallback_attempts.append(attempt)
                body = self._verify_end(get_numbering)
                attempt.update(verified=body is not None, numbering=get_numbering().diagnostics())
                if body:
                    return body
            self._return_to_list()
        else:
            return
        if (get_numbering is not None and selected.index > 0
                and self.connect_core.config.screen_height > FALLBACK_HEIGHT):
            self.operations['short_terminal_retries'] += 1
            attempt = {'height': FALLBACK_HEIGHT, 'method': 'short_terminal'}
            fallback_attempts.append(attempt)
            body = self._read_at_fallback_height(selected, get_numbering)
            attempt.update(verified=body is not None, numbering=get_numbering().diagnostics())
            if body:
                return body
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

    def get_post(self, board, *, index=None, aid=None, query=False, get_numbering=None, fallback_attempts=None):
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
            if self.capture_mode == 'page':
                body = self._read_article_pages(get_numbering)
                has_control_code = False
                self.api.Unconfirmed = False
            elif get_numbering is not None and self.connect_core.config.screen_height == FALLBACK_HEIGHT:
                body = self._read_numbered_article(get_numbering)
                numbering = get_numbering()
                fallback_attempts.append({'height': FALLBACK_HEIGHT, 'method': 'short_terminal_from_login',
                                          'verified': body is not None, 'numbering': numbering.diagnostics()})
                if body is None:
                    self._return_to_list()
                    raise ArticleStalled(numbering.last_window[1] if numbering.last_window else None)
                has_control_code = False
                self.api.Unconfirmed = False
            else:
                try:
                    body, has_control_code = _api_util.get_content(self.api)
                except exceptions.UnknownError as exc:
                    body = self._recover_stalled_article(exc, selected, get_numbering, fallback_attempts)
                    if body is None:
                        raise
                    has_control_code = False
                else:
                    self.view = 'article'
            if body is None:
                # The current screen may be a prompt, so leave navigation to a new session.
                raise BoardNavigationError()
            post.update({PostField.full_content: body, PostField.has_control_code: has_control_code,
                         PostField.is_unconfirmed: self.api.Unconfirmed})
            if len(body.split('\n', 3)) >= 3:
                post = _api_get_post._parse_post_content(self.api, post, board, body)
        return json.loads(json.dumps(post, cls=AutoJsonEncoder))
