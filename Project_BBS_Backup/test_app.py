import tempfile
import unittest
import sqlite3
import re
import json
from unittest.mock import patch
from pathlib import Path
import app

class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch('local_state.LOG_DIRECTORY', Path(temporary) / 'log'))
        self.enterContext(patch.object(app, 'BoardSession', side_effect=lambda api: api))

    def test_batch_bounds_start_at_requested_article_in_either_direction(self):
        for newest, start, count, direction, expected in [
            (2218, 2179, 30, 'newest', (2150, 2179)),
            (2218, 2179, 30, 'oldest', (2179, 2208)),
            (2218, 2148, 100, 'newest', (2049, 2148)),
            (2218, 0, 30, 'newest', (2189, 2218)),
            (2218, 0, 30, 'oldest', (2218, 2218)),
            (2218, 12, 30, 'newest', (1, 12)),
            (2218, 2210, 30, 'oldest', (2210, 2218)),
            (2218, 2219, 30, 'newest', None),
            (0, 0, 30, 'newest', None),
        ]:
            with self.subTest(newest=newest, start=start, count=count, direction=direction):
                self.assertEqual(app.batch_bounds(newest, start, count, direction), expected)

    def test_board_screen_diagnostic_distinguishes_headers_and_access_without_screen_text(self):
        from types import SimpleNamespace

        core = SimpleNamespace(last_timeout_was_silent=False)
        api = SimpleNamespace(connect_core=core, cursor='>')
        for screen, kind, flexible in [
            ('看板 YP91-304\n編號  日  期 作 者\n> private-title', 'article_list', True),
            ('★ 您沒有權限進入此看板\nprivate-notice', 'access_notice', False),
            ('查無此看板', 'missing_board_notice', False),
        ]:
            with self.subTest(kind=kind):
                core.get_screen_queue = lambda: [screen]
                diagnostic = app.board_screen_diagnostic(api)
                self.assertEqual(diagnostic['screen_kind'], kind)
                self.assertEqual(diagnostic['flexible_board_header'], flexible)
                self.assertFalse(diagnostic['strict_board_header'])
                self.assertNotIn('private-', json.dumps(diagnostic))

    def test_trailing_terminal_padding_does_not_extend_article(self):
        from archive import article_body

        self.assertEqual(article_body('正文\n\n  \x08\n', None),
                         '<pre style="--frame-rows: 22; --frame-padding: 21lh">\n正文</pre>')
        color_lines = {'1': [('  ', ('b6',))], '2': [('  ', ('b0',))]}
        colored = article_body('正文\n  \n  \n\n', color_lines)
        self.assertTrue(colored.endswith('<span class="ansi-run b6">  </span></pre>'))
        self.assertNotIn('class="ansi-run b0"', colored)

    def test_destination_creates_board_path_and_recognizes_matching_legacy_archive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            child = root / 'PTT_YP91-304'
            self.assertEqual(app.archive_destination(root, 'PTT', 'YP91-304'), child)
            self.assertEqual(app.archive_destination(child, 'PTT', 'YP91-304'), child)
            db = sqlite3.connect(root / 'articles.sqlite3')
            db.execute('''CREATE TABLE batch_ranges(site TEXT, board TEXT, first_index INTEGER,
                          last_index INTEGER, completed INTEGER, PRIMARY KEY(site,board))''')
            db.execute("INSERT INTO batch_ranges VALUES('PTT','YP91-304',1,10,0)")
            db.commit()
            db.close()
            self.assertEqual(app.archive_destination(root, 'PTT', 'YP91-304'), root)
            self.assertEqual(app.archive_destination(root, 'PTT2', 'YP91-304'), root / 'PTT2_YP91-304')
            self.assertEqual(app.find_pending_range(root, 'PTT', 'YP91-304'), (1, 10, 'newest'))
            db = app.database(root)
            self.assertEqual(db.execute('SELECT direction FROM batch_ranges').fetchone(), ('newest',))
            db.close()

    def test_ansi_cursor_gaps_stay_black_but_authored_spaces_keep_color(self):
        from ansi_capture import ColorScreen

        raw = ('\x1b[1;33;41m\x1b[1;6H這是測試\x1b[m\n'
               '\x1b[37;41m  有底色的空白\x1b[m\n'
               '\x1b[30;46m\x1b[3;14H█').encode()
        lines = ColorScreen(3).replay(raw, 'utf-8')
        self.assertEqual(lines[0].runs(), [('     ', ()), ('這是測試', ('f3', 'b1', 'hl'))])
        self.assertEqual(lines[1].runs(), [('  有底色的空白', ('f7', 'b1'))])
        self.assertEqual(lines[2].runs(), [('             ', ()), ('█', ('f0', 'b6'))])

    def test_ansi_capture_and_archive_preserve_two_pages(self):
        from types import SimpleNamespace
        from PyPtt import screens
        from PyPtt.connect_core import TargetUnit
        from ansi_capture import read_colored_post

        class FakeCore:
            config = SimpleNamespace(screen_height=5)
            current_encoding = 'utf-8'
            pages = iter([
                '\x1b[H\x1b[2J標題\n\x1b[1;33;41m彩色\x1b[m\n\n重疊列\n瀏覽 第 1/2 頁 目前顯示: 第 1~4 行',
                '\x1b[H\x1b[2J重疊列\n\x1b[36;44m████\x1b[m\n末段\n\n瀏覽 第 2/2 頁 (100%) 目前顯示: 第 4~6 行',
            ])

            def send(self, message, targets):
                self._stream_parsers = {}
                data = next(self.pages).encode('utf-8')
                self._stream_screen('utf-8', data[:13])
                self._screen = self._stream_screen('utf-8', data[13:])
                return 0

            def _stream_screen(self, encoding, data):
                if encoding not in self._stream_parsers:
                    self._stream_parsers[encoding] = screens.IncrementalScreen(encoding, self.config.screen_height)
                parser = self._stream_parsers[encoding]
                parser.feed(data)
                return parser.screen

            def get_screen_queue(self):
                return [self._screen]

        core = FakeCore()

        def get_post(board, aid):
            core.send('', [TargetUnit(screens.Target.InPost)])
            core.send('', [TargetUnit(screens.Target.PostEnd)])
            return {'aid': aid, 'title': '標題', 'full_content': '標題\n彩色\n重疊列\n████\n末段',
                    'pass_format_check': True}

        api = SimpleNamespace(connect_core=core, get_post=get_post)
        post = read_colored_post(api, 'Test', '1AdzogFB')
        self.assertEqual(post['full_content'], '標題\n彩色\n\n重疊列\n████\n末段')
        self.assertEqual(post['ansi_coverage'], {'matched': 6, 'total': 6, 'colored': 2})
        diagnostics = post['ansi_diagnostics']
        self.assertTrue(diagnostics['reached_end'])
        self.assertEqual(diagnostics['assembly'], 'numbered')
        self.assertEqual(diagnostics['sdk_lines'], 5)
        self.assertEqual(diagnostics['selected_blank_lines'], 1)
        self.assertEqual(diagnostics['first_changed_line'], 3)
        self.assertEqual(post['ansi_lines']['1'], [('彩色', ('f3', 'b1', 'hl'))])
        self.assertEqual(post['ansi_lines']['4'], [('████', ('f6', 'b4'))])
        self.assertEqual(core.send.__func__, FakeCore.send)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            db = app.database(folder)
            app.save_article(db, 'Test', post)
            app.export_html(db, folder)
            db.close()
            result = next((folder / 'articles').glob('*.html')).read_text(encoding='utf-8')
        self.assertIn('<span class="ansi-run f3 b1 hl">彩色</span>', result)
        self.assertEqual(result.count('<span class="block-cell">█</span>'), 4)
        self.assertIn('彩色</span>\n\n重疊列', result)
        self.assertEqual(result.count('重疊列'), 1)

    def test_numbered_article_rejects_gaps_and_conflicting_overlaps(self):
        from ansi_capture import NumberedArticle

        article = NumberedArticle()
        article.observe(['one', ''], '瀏覽 第 1/3 頁 目前顯示: 第 1~2 行', at_end=False, styled=None)
        article.observe(['four', 'five'], '瀏覽 第 3/3 頁 目前顯示: 第 4~5 行', at_end=True, styled=None)
        self.assertIsNone(article.assemble())
        self.assertEqual(article.diagnostics()['missing_ranges_sample'], [[3, 3]])
        article.observe(['different', 'three'], '瀏覽 第 2/3 頁 目前顯示: 第 2~3 行', at_end=False, styled=None)
        self.assertIsNone(article.assemble())
        self.assertEqual(article.diagnostics()['first_conflict_line'], 2)

    def test_headerless_article_requires_confirmed_end(self):
        post = {'aid': '1DTwb0l7', 'title': 'Animation', 'full_content': '第一格\n第二格',
                'pass_format_check': False, 'ansi_coverage': {'matched': 1, 'total': 2, 'colored': 0}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'STATE', {'saved': 0}), \
                patch('ansi_capture.read_colored_post') as capture:
            folder = Path(tmp)
            db = app.database(folder)
            capture.return_value = dict(post, ansi_diagnostics={'reached_end': False})
            with self.assertRaises(app.ArticleReadError):
                app.download_article(None, db, 'PTT', 'Test', post['aid'])
            capture.return_value = dict(post, ansi_diagnostics={'reached_end': True})
            outcome, verified = app.download_article(None, db, 'PTT', 'Test', post['aid'])
            self.assertEqual(outcome, 'saved')
            self.assertFalse(verified)
            self.assertEqual(app.export_html(db, folder), 1)
            db.close()
            article = next((folder / 'articles').glob('*.html')).read_text(encoding='utf-8')
            self.assertIn('第一格\n第二格</pre>', article)

    def test_archive_pages_are_counted_back_from_latest_article(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            db = app.database(folder)
            for index in range(45, 0, -1):
                app.save_article(db, 'Test', {
                    'aid': f'aid{index}', 'title': f'Article {index}',
                    'full_content': 'Example', 'pass_format_check': True,
                }, site='PTT', source_index=index)
            app.export_html(db, folder)
            db.close()

            index_page = (folder / 'index.html').read_text(encoding='utf-8')
            pages = re.findall(r'<ul class="entries"( hidden)?>(.*?)</ul>', index_page)
            self.assertEqual([bool(hidden) for hidden, _ in pages], [True, True, False])
            numbers = [[int(number) for number in re.findall(r'Article (\d+)</a>', body)]
                       for _, body in pages]
            self.assertEqual(numbers, [list(range(1, 6)), list(range(6, 26)), list(range(26, 46))])

            links = dict((int(number), path) for path, number in re.findall(
                r'href="(articles/[^\"]+)"><span[^>]*>[^<]*</span>Article (\d+)</a>', index_page))
            boundary = (folder / links[26]).read_text(encoding='utf-8')
            self.assertIn(f'id="previous" href="{Path(links[25]).name}"', boundary)
            self.assertIn(f'id="next" href="{Path(links[27]).name}"', boundary)
            latest = (folder / links[45]).read_text(encoding='utf-8')
            self.assertNotIn('id="next"', latest)

    def test_dedup_export_and_partial_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            db=app.database(folder)
            post={'aid':'example','title':'<script>','full_content':'中文\n推 test: 重複\n推 test: 重複','pass_format_check':True}
            app.save_article(db,'Test',post)
            app.save_article(db,'Test',post)
            self.assertEqual(db.execute('SELECT count(*) FROM articles').fetchone()[0],1)
            with self.assertRaises(ValueError):
                app.save_article(db,'Test',{'aid':'bad','full_content':'partial'})
            app.export_html(db,folder)
            result=(folder/'index.html').read_text(encoding='utf-8')
            self.assertIn('&lt;script&gt;',result)
            self.assertNotIn('推 test: 重複',result)
            article, = (folder/'articles').glob('*.html')
            self.assertIn(f'articles/{article.name}',result)
            self.assertEqual(article.read_text(encoding='utf-8').count('推 test: 重複'),2)
            db.close()

    def test_existing_ptt2_archive_and_same_aid_on_ptt(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            old = sqlite3.connect(folder / 'articles.sqlite3')
            old.execute('CREATE TABLE articles(board TEXT, aid TEXT, title TEXT, body TEXT, data TEXT, fetched REAL, PRIMARY KEY(board, aid))')
            old.execute("INSERT INTO articles VALUES ('Test', 'sameAid', '舊文章', '舊內文', '{}', 1)")
            old.execute('CREATE TABLE failures(board TEXT, idx INTEGER, reason TEXT, created REAL)')
            old.execute("INSERT INTO failures VALUES ('Test', 1, '舊紀錄', 1)")
            old.commit()
            old.close()
            db = app.database(folder)
            post = {'aid': 'sameAid', 'title': '新文章', 'full_content': '新內文', 'pass_format_check': True}
            app.save_article(db, 'Test', post, site='PTT', source_index=2)
            app.save_article(db, 'Test', {**post, 'aid': 'olderAid'}, site='PTT', source_index=1)
            self.assertEqual(db.execute('SELECT site FROM failures').fetchone(), ('PTT2',))
            self.assertEqual(app.export_html(db, folder), 3)
            db.close()
            pages = [f.read_text(encoding='utf-8') for f in (folder / 'articles').glob('*.html')]
            self.assertEqual(len(pages), 3)
            self.assertTrue(any('舊內文' in page for page in pages))
            self.assertEqual(sum('id="next"' in page for page in pages), 1)
            self.assertEqual(sum('id="previous"' in page for page in pages), 1)

    def test_ptt_new_batch_rereads_and_detects_edits(self):
        import PyPtt

        with tempfile.TemporaryDirectory() as d, patch.object(PyPtt, 'API') as factory, \
                patch.object(app.STOP, 'wait', return_value=False), patch.object(app, 'STATE', {}), \
                patch('ansi_capture.read_colored_post', side_effect=lambda api, board, aid: api.get_post(board, aid=aid)):
            api = factory.return_value
            api.get_newest_index.return_value = 1
            api.get_post.return_value = {
                'aid': 'someAid', 'title': '標題', 'full_content': '中文', 'pass_format_check': True,
                'ansi_coverage': {'total': 1, 'matched': 1, 'colored': 0},
            }
            config = dict(site='PTT', board='Test', start=0, count=5, folder=d, user='test', password='dummy')
            for local, saved, skipped, body, color, aid in [
                (0, 1, 0, '中文', 'f1', ''),
                (1, 0, 1, '中文', 'f1', 'someAid'),
                (1, 1, 0, '中文', 'f2', ''),
                (1, 1, 0, '作者已編輯', 'f2', ''),
            ]:
                api.get_post.return_value.update(full_content=body, ansi_lines={'0': [[body, [color]]]})
                app.STATE.update(updated=0, saved=0, skipped=0, unresolved=0, timed_samples=0)
                app.work(dict(config, aid=aid))
                self.assertEqual((app.STATE['saved'], app.STATE['skipped']), (saved, skipped))
                self.assertEqual((app.STATE['local_count'], app.STATE['updated']), (local, saved))
            self.assertEqual(factory.call_args.kwargs['host'], PyPtt.HOST.PTT1)
            self.assertEqual(api.get_post.call_count, 7)
            db = app.database(Path(d))
            self.assertEqual(db.execute('SELECT body FROM articles').fetchone(), ('作者已編輯',))
            db.close()

    def test_resume_retains_range_and_direction_after_new_articles_arrive(self):
        import PyPtt

        with tempfile.TemporaryDirectory() as d, patch.object(PyPtt, 'API') as factory, \
                patch.object(app.STOP, 'wait', side_effect=[False, True, False, False, False]), \
                patch.object(app, 'STATE', {}), \
                patch('ansi_capture.read_colored_post', side_effect=lambda api, board, aid: api.get_post(board, aid=aid)):
            api = factory.return_value
            api.get_newest_index.side_effect = [10, 12]

            def get_post(board, *, index=None, aid=None, query=False):
                return {'aid': f'aid{index}' if query else aid, 'title': '標題',
                        'full_content': '中文', 'pass_format_check': True,
                        'ansi_coverage': {'total': 1, 'matched': 1, 'colored': 0}}

            api.get_post.side_effect = get_post
            config = dict(site='PTT', board='Test', start=9, count=2, folder=d, user='test', password='dummy')
            for resumed, expected in [(False, (1, 0, 1)), (True, (1, 0, 2))]:
                app.STATE.update(saved=0, skipped=0, unresolved=0, timed_samples=0, resumed=False, resumed_verified=0)
                app.work(dict(config, direction='newest' if resumed else 'oldest'))
                self.assertEqual((app.STATE['batch_start'], app.STATE['batch_end']), (9, 10))
                self.assertEqual((app.STATE['saved'], app.STATE['skipped'], app.STATE['scanned']), expected)
                self.assertEqual(app.STATE['resumed'], resumed)
                self.assertEqual(app.STATE['direction'], 'oldest')
                self.assertEqual(app.STATE['resumed_verified'], int(resumed))
            queries = [call.kwargs['index'] for call in api.get_post.call_args_list if call.kwargs.get('query')]
            self.assertEqual(queries, [9, 9, 10])

    def test_preferences_exclude_password_and_logs_exclude_exception_text(self):
        from local_state import JobLog, load_preferences, save_preferences

        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            with patch('local_state.SETTINGS', folder / 'form.json'):
                save_preferences({'site': 'PTT', 'user': 'example', 'password': 'secret-password',
                                  'board': 'Test', 'aid': 'someAid1'})
                self.assertEqual(load_preferences(), {'site': 'PTT', 'user': 'example', 'board': 'Test'})
                self.assertNotIn('secret-password', (folder / 'form.json').read_text())
            job = JobLog(folder)
            try:
                raise ValueError('secret-password and private screen contents')
            except ValueError as exc:
                job.error('read_article', exc, index=2213, aid='example')
            log = job.path.read_text(encoding='utf-8')
            self.assertIn('read_article', log)
            self.assertIn('2213', log)
            self.assertNotIn('secret-password', log)

    def test_batch_resumes_text_only_articles_and_retries_uncertain_results(self):
        import PyPtt
        from PyPtt import exceptions

        with tempfile.TemporaryDirectory() as d, patch.object(PyPtt, 'API') as factory, \
                patch.object(app.STOP, 'wait', return_value=False), patch.object(app, 'STATE', {}), \
                patch('ansi_capture.read_colored_post') as capture:
            api = factory.return_value
            api.cursor = '>'
            api.get_newest_index.return_value = 7
            bodies = []

            def query(board, *, index, query):
                if index in (4, 6) and attempt < 2:
                    # A stale normal listing must never be accepted as a deletion.
                    screen = '> 4 11/10 - □ (本文已被刪除)' if index == 4 else '> 6 11/10 Test □ Normal'
                    api.connect_core.get_screen_queue.return_value = [screen]
                    return {'post_status': 'DELETED_BY_UNKNOWN'}
                if index == 4:
                    api.connect_core.get_screen_queue.return_value = ['> 4 11/10 - □ (本文已被刪除)']
                    return {'post_status': 'DELETED_BY_UNKNOWN'}
                return {'aid': f'article{index}', 'index': index, 'is_lock': index == 5}

            def read(api, board, aid):
                index = int(aid[-1])
                bodies.append((attempt, index))
                if index == 3 and attempt == 0:
                    raise exceptions.ConnectionClosed.__new__(exceptions.ConnectionClosed)
                if index == 7 and attempt == 1:
                    return {'aid': aid, 'index': index}
                return {'aid': aid, 'index': index, 'title': aid, 'full_content': '正文',
                        'pass_format_check': True,
                        'ansi_coverage': {'matched': int(index != 2), 'total': 1, 'colored': 0}}

            api.get_post.side_effect = query
            capture.side_effect = read
            folder = Path(d)
            config = dict(site='PTT', board='Test', start=1, count=7, direction='oldest',
                          folder=d, user='test', password='dummy')
            for attempt, result, expected in [
                (0, 'disconnected', (2, 0, 0, 0, 1)),
                (1, 'pending', (1, 2, 2, 2, 0)),
                (2, 'done', (2, 3, 2, 0, 0)),
            ]:
                app.STATE.clear()
                app.STATE.update(saved=0, skipped=0, unresolved=0, timed_samples=0)
                app.work(dict(config, mode='resume' if attempt else 'new'))
                self.assertEqual(app.STATE['result'], result, app.STATE['message'])
                self.assertEqual(tuple(app.STATE.get(key, 0) for key in (
                    'saved', 'resumed_verified', 'unavailable', 'unresolved', 'ansi_review')), expected)
                self.assertEqual(bool(app.find_pending_range(folder, 'PTT', 'Test')), attempt < 2)
                events = [json.loads(line) for line in
                          Path(app.STATE['log_path']).read_text(encoding='utf-8').splitlines()]
                plan = next(event for event in events if event['event'] == 'range')
                self.assertEqual((plan['first_scan'], plan['last_scan'], plan['planned_count']), (1, 7, 7))
                self.assertEqual(events[-1]['remaining_scan'], 5 if not attempt else 0)
                articles = [event for event in events if event['event'] == 'article']
                self.assertTrue(all('seconds' in item and 'query_seconds' in item for item in articles))
                if not attempt:
                    self.assertEqual((articles[-1]['index'], articles[-1]['error']), (3, 'ConnectionClosed'))
                    self.assertEqual(events[-1]['pending_color'], 1)

            self.assertEqual(bodies, [(0, 1), (0, 2), (0, 3), (1, 3), (1, 7), (2, 6), (2, 7)])
            self.assertEqual(app.STATE['exported'], 5)
            index = (folder / 'index.html').read_text(encoding='utf-8')
            for text in ('article2', '（被刪除）', '（已鎖定）'):
                self.assertIn(text, index)
            self.assertIn('2\tarticle2\t0/1', (folder / 'color_line_check.txt').read_text(encoding='utf-8-sig'))

    def test_batch_refreshes_legacy_article_without_ansi_capture(self):
        import PyPtt

        with tempfile.TemporaryDirectory() as d, patch.object(PyPtt, 'API') as factory, \
                patch.object(app.STOP, 'wait', return_value=False), patch.object(app, 'STATE', {}), \
                patch('ansi_capture.read_colored_post') as capture:
            folder = Path(d)
            post = {'aid': 'sample', 'title': '標題', 'full_content': '中文', 'pass_format_check': True}
            db = app.database(folder)
            app.save_article(db, 'Test', post, site='PTT')
            db.close()
            api = factory.return_value
            api.get_newest_index.return_value = 1
            api.get_post.return_value = {'aid': 'sample'}
            capture.return_value = dict(post, ansi_coverage={'total': 1, 'matched': 1, 'colored': 1},
                                        ansi_lines={'0': [('中文', ('f3',))]})
            app.STATE.update(saved=0, skipped=0, unresolved=0, timed_samples=0)
            app.work(dict(site='PTT', board='Test', start=0, count=1, folder=d, user='test', password='dummy'))
            capture.assert_called_once_with(api, 'Test', 'sample')
            self.assertEqual(app.STATE['ansi_colored'], 1)
            self.assertEqual(len(list((folder / 'articles').glob('*.html'))), 1)

    def test_automatic_rest_can_stop_and_resume_past_a_persistent_gap(self):
        import PyPtt

        with tempfile.TemporaryDirectory() as d, patch.object(PyPtt, 'API') as factory, \
                patch.object(app.STOP, 'wait') as wait, patch.object(app, 'STATE', {}), \
                patch('ansi_capture.read_colored_post') as capture:
            api = factory.return_value
            api.get_newest_index.return_value = 52
            api.get_post.side_effect = lambda board, index, query: {'aid': f'article{index}' if index != 1 else None}
            capture.side_effect = lambda api, board, aid: {
                'aid': aid, 'full_content': '正文', 'pass_format_check': True,
                'ansi_coverage': {'matched': 1, 'total': 1, 'colored': 0},
            }
            config = dict(site='PTT', board='Test', start=1, count=52, direction='oldest',
                          folder=d, user='test', password='dummy')
            for stop_at_rest, expected, rests in [(True, ('stopped', 24), 1), (False, ('pending', 51), 1)]:
                app.STATE.clear()
                app.STATE.update(saved=0, skipped=0, unresolved=0, timed_samples=0)
                wait.side_effect = lambda seconds: stop_at_rest and seconds == app.REST_SECONDS
                app.work(config.copy())
                self.assertEqual((app.STATE['result'], app.STATE['exported']), expected)
                rows = [json.loads(line) for line in
                        Path(app.STATE['log_path']).read_text(encoding='utf-8').splitlines()]
                self.assertEqual(rows[-1]['rest_count'], rests)
                self.assertEqual(sum(row['event'] == 'pacing_rest' for row in rows), rests)
            self.assertEqual(capture.call_count, 51)
            self.assertEqual(api.login.call_count, 2)

    def test_rate_limit_preserves_batch_and_closes_without_logout_keys(self):
        from datetime import datetime, timedelta
        import PyPtt
        from board_session import StationRateLimit, TAIPEI

        with tempfile.TemporaryDirectory() as d, patch.object(PyPtt, 'API') as factory, \
                patch.object(app, 'STATE', dict(saved=0, skipped=0, unresolved=0)), \
                patch.object(app, 'RATE_LIMITS', {}), patch('local_state.RATE_LIMIT_FILE', Path(d) / 'limits.json'):
            api = factory.return_value
            api.get_newest_index.return_value = 2
            deadline = (datetime.now(TAIPEI) + timedelta(hours=1)).isoformat(timespec='seconds')
            limit = {'until': deadline, 'station_board_entries': 2666}
            api.get_post.side_effect = StationRateLimit(limit)
            app.work(dict(site='PTT', board='Test', start=0, count=2, direction='newest',
                          folder=d, user='test', password='dummy'))
            self.assertEqual(app.STATE['result'], 'rate_limited')
            self.assertEqual(app.find_pending_range(Path(d), 'PTT', 'Test'), (1, 2, 'newest'))
            self.assertEqual(app.active_rate_limits()['PTT'], limit)
            self.assertEqual(json.loads((Path(d) / 'limits.json').read_text()), {'PTT': limit})
            api.logout.assert_not_called()
            api.connect_core.close.assert_called_once()

    def test_batch_continues_after_a_safely_recovered_stall(self):
        import PyPtt
        from board_session import ArticleStalled

        with tempfile.TemporaryDirectory() as d, patch.object(PyPtt, 'API') as factory, \
                patch.object(app.STOP, 'wait', return_value=False), \
                patch.object(app, 'STATE', dict(saved=0, skipped=0, unresolved=0, timed_samples=0)), \
                patch('ansi_capture.read_colored_post') as capture:
            api = factory.return_value
            api.get_newest_index.return_value = 3
            api.get_post.side_effect = lambda board, index, query: {'aid': f'article{index}', 'index': index}

            def read(api, board, aid):
                if aid == 'article2':
                    exc = ArticleStalled(103)
                    exc.capture_diagnostics = {'reached_end': False}
                    raise exc
                return {'aid': aid, 'full_content': '正文', 'pass_format_check': True,
                        'ansi_coverage': {'matched': 1, 'total': 1, 'colored': 0}}

            capture.side_effect = read
            app.work(dict(site='PTT', board='Test', start=1, count=3, direction='oldest',
                          folder=d, user='test', password='dummy'))
            self.assertEqual((app.STATE['result'], app.STATE['scanned'], app.STATE['exported']), ('pending', 3, 2))
            self.assertEqual(app.find_pending_range(Path(d), 'PTT', 'Test'), (1, 3, 'oldest'))
            rows = [json.loads(line) for line in Path(app.STATE['log_path']).read_text(encoding='utf-8').splitlines()]
            self.assertEqual(next(row['stalled_line'] for row in rows if row.get('outcome') == 'unreadable'), 103)
            api.login.assert_called_once()

class BoardSessionTests(unittest.TestCase):
    def test_station_wait_retries_once_with_no_relogin(self):
        from datetime import datetime, timedelta
        from unittest.mock import Mock
        from board_session import StationRateLimit, TAIPEI

        until = datetime.now(TAIPEI) + timedelta(minutes=5)
        notice = StationRateLimit({'until': until.isoformat()})
        session = Mock(notice=notice)
        session.recover_notice.return_value = True
        action = Mock(side_effect=notice)
        retry = Mock(return_value='same connection')
        log = Mock()
        with patch.object(app, 'STATE', {'phase': 'download'}), patch.object(app, 'RATE_LIMITS', {}), \
                patch.object(app, 'save_rate_limits'), patch.object(app, 'load_rate_limits', return_value={}):
            self.assertEqual(app.station_action(session, 'Test', 'PTT', log, action, retry=retry),
                             'same connection')
            self.assertEqual(app.STATE['phase'], 'download')
            self.assertIsNone(app.STATE['wait_until'])
        session.recover_notice.assert_called_once_with('Test', until, app.STOP)
        action.assert_called_once()
        retry.assert_called_once()

    def test_recover_notice_waits_on_same_socket_before_acknowledging(self):
        import asyncio
        import threading
        from datetime import datetime, timedelta
        from types import SimpleNamespace
        from unittest.mock import Mock
        from board_session import BoardSession, BoardTemporarilyClosed, TAIPEI
        from PyPtt import command

        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        events = []
        listing = '【看板】 [i]看板設定\n> 9 test\n文章列表 (b)進板'

        async def recv():
            events.append('receive')
            await asyncio.sleep(5)

        def send(keys, targets, **kwargs):
            events.append(keys)
            return 0

        core = SimpleNamespace(_decode_screen=Mock(), send=send, _recv=recv,
                               _get_event_loop=lambda: loop, get_screen_queue=lambda: [listing])
        session = BoardSession(SimpleNamespace(connect_core=core, cursor='>'))
        with self.assertRaises(BoardTemporarilyClosed):
            core._decode_screen('看板暫時關閉\n請按任意鍵繼續')
        until = datetime.now(TAIPEI) - timedelta(seconds=1)
        self.assertTrue(session.recover_notice('Test', until, threading.Event()))
        self.assertEqual(events, ['receive', command.enter])
        self.assertEqual((session.board, session.view), ('Test', 'list'))

    def test_recovering_known_stall_sends_one_left_and_requires_list(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from board_session import ArticleStalled, BoardSession
        from PyPtt import command, exceptions

        frames = ['瀏覽 第 1/2 頁 目前顯示: 第 5~103 行']
        keys = []

        def send(message, targets):
            keys.append(message)
            frames[-1] = '【看板】 [i]看板設定\n> 9 test\n文章列表 (b)進板'
            return 0

        core = SimpleNamespace(_decode_screen=Mock(), send=send, get_screen_queue=lambda: frames)
        session = BoardSession(SimpleNamespace(connect_core=core, cursor='>'))
        with self.assertRaises(ArticleStalled) as caught:
            session._recover_stalled_article(exceptions.UnknownError('get_content: stuck at line 103'))
        self.assertEqual(caught.exception.line, 103)
        self.assertEqual(keys, [command.left])
        self.assertEqual(session.view, 'list')

    def test_reads_adjacent_articles_with_one_board_entry_and_no_aid_search(self):
        from types import SimpleNamespace
        import PyPtt
        from PyPtt import command, connect_core, screens
        from board_session import BoardSession

        class Terminal:
            last_timeout_was_silent = False

            def __init__(self):
                self.keys = []
                self.index = 12
                self.screen = ''

            def send(self, keys, targets, **kwargs):
                self.keys.append(keys)
                if keys == command.up:
                    self.index -= 1
                if keys == 'Q':
                    self.screen = '文章代碼(AID):\n文章網址:\n請按任意鍵繼續'
                elif keys == command.enter * 2:
                    self.screen = '瀏覽 第 1/1 頁 (100%) 目前顯示: 第 1~1 行'
                else:
                    self.screen = f'【看板】 [i]看板設定\n> {self.index} test\n文章列表 (b)進板'
                self._decode_screen(self.screen)
                return next((i for i, target in enumerate(targets) if target.is_match(self.screen, '>')), -1)

            def _decode_screen(self, screen):
                return screen

            def get_screen_queue(self):
                return [self.screen]

        api = PyPtt.API(check_update=False, screen_height=100)
        api.cursor = '>'
        api.connect_core = core = Terminal()
        session = BoardSession(api)

        def selected(*args):
            return SimpleNamespace(index=core.index, aid=f'item{core.index}', author='tester',
                                   title='article', url=None, money=0, list_date=None,
                                   push_number=None, lock_post=False)

        def content(reader):
            reader.connect_core.send(command.enter * 2, [connect_core.TargetUnit(screens.Target.PostEnd)])
            reader.Unconfirmed = False
            return '正文', False

        with patch('board_session.MIN_COMMAND_INTERVAL', 0), \
                patch('board_session._api_get_newest_index._get_newest_index', return_value=12), \
                patch('board_session._api_util.parse_query_post', side_effect=selected), \
                patch('board_session._api_util.get_content', side_effect=content):
            self.assertEqual(session.get_newest_index(None, board='Test'), 12)
            for index in (12, 11):
                post = session.get_post('Test', index=index, query=True)
                self.assertEqual(post['aid'], f'item{index}')
                self.assertEqual(session.get_post('Test', aid=post['aid'])['full_content'], '正文')
        self.assertEqual(session.operations['board_entries'], 1)
        self.assertEqual(session.operations['board_searches'], 1)
        self.assertEqual(session.operations['aid_searches'], 0)
        self.assertEqual(session.operations['article_opens'], 2)
        self.assertEqual(core.keys[1:], ['$', 'Q', command.enter * 2, command.left, command.up,
                                        'Q', command.enter * 2])

    def test_rate_limit_is_intercepted_before_automatic_response(self):
        from datetime import datetime
        from types import SimpleNamespace
        from unittest.mock import Mock
        from board_session import BoardSession, StationRateLimit, TAIPEI, detect_rate_limit

        screen = ('使用限制   限速中\n切換看板、搜尋和閱讀文章暫時限速中，預計 11/10 17:03 恢復\n'
                  '紀錄：1.0小時內進入看板2666次、搜尋337次\n請按任意鍵繼續')
        limit = detect_rate_limit(screen, datetime(2026, 11, 10, 16, 30, tzinfo=TAIPEI))
        self.assertEqual(limit['until'], '2026-11-10T17:04:00+08:00')
        self.assertEqual((limit['station_board_entries'], limit['station_searches']), (2666, 337))
        decode, send = Mock(), Mock()
        core = SimpleNamespace(_decode_screen=decode, send=send)
        BoardSession(SimpleNamespace(connect_core=core))
        with self.assertRaises(StationRateLimit):
            core._decode_screen(screen)
        with self.assertRaises(StationRateLimit):
            core.send(' ', [])
        decode.assert_not_called()
        send.assert_not_called()


if __name__=='__main__': unittest.main()
