import tempfile
import unittest
from pathlib import Path
import app

class StorageTests(unittest.TestCase):
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
            self.assertEqual(result.count('推 test: 重複'),2)
            db.close()

if __name__=='__main__': unittest.main()
