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

if __name__=='__main__': unittest.main()
