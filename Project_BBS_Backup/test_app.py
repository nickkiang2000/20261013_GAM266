import tempfile
import unittest
import sqlite3
import re
from unittest.mock import patch
from pathlib import Path
import app

class StorageTests(unittest.TestCase):
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

    def test_ptt_download_and_rerun_uses_site_specific_dedup(self):
        import PyPtt

        with tempfile.TemporaryDirectory() as d, patch.object(PyPtt, 'API') as factory, \
                patch.object(app.STOP, 'wait', return_value=False), patch.object(app, 'STATE', {}):
            api = factory.return_value
            api.get_newest_index.return_value = 1
            api.get_post.return_value = {
                'aid': 'someAid', 'title': '標題', 'full_content': '中文', 'pass_format_check': True
            }
            config = dict(site='PTT', board='Test', start=0, count=5, folder=d, user='test', password='dummy')
            for saved, skipped in [(1, 0), (0, 1)]:
                app.STATE.update(saved=0, skipped=0, unresolved=0, timed_samples=0)
                app.work(config.copy())
                self.assertEqual((app.STATE['saved'], app.STATE['skipped']), (saved, skipped))
            self.assertEqual(factory.call_args.kwargs['host'], PyPtt.HOST.PTT1)
            self.assertEqual(api.get_post.call_count, 3)

if __name__=='__main__': unittest.main()
