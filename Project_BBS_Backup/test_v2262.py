import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from PyPtt.lib_util import get_aid_from_url
import app
import archive
import board_metadata
from article_metadata import posting_date, author_id, parse_date

URL = 'https://www.ptt.cc/bbs/SampleBoard/M.1106348748.A.html'
AID = get_aid_from_url(URL)[1]


class MetadataTests(unittest.TestCase):
    def test_dates_accept_station_formats_and_validate_calendar(self):
        for value in ('Sat Jan 22 08:25:48 2005', '01/22/2005 08:25:48 Sat', '2005-01-22T00:25:48+00:00'):
            self.assertEqual(posting_date({'date': value})[0], '2005/01/22')
        self.assertIsNone(parse_date('Fri Feb 30 08:00:00 2005'))
        self.assertEqual(author_id('SampleUser (nickname)'), 'SampleUser')

    def test_aid_and_list_date_recover_year_without_using_body_or_neighbors(self):
        date, source = posting_date({'list_date': '1/22'}, site='PTT2', board='SampleBoard', aid=AID)
        self.assertEqual(date, '2005/01/22')
        self.assertIn('列表', source)
        self.assertEqual(posting_date({'list_date': '1/23'}, site='PTT2', aid=AID)[0], '01/23（年份待查）')
        self.assertEqual(posting_date({'list_date': '1/22'}, site='CCNS', aid=AID)[0], '01/22（年份待查）')
        self.assertEqual(posting_date({}, site='PTT', aid=AID)[0], '日期待查')

    def test_url_requires_matching_station_board_and_aid(self):
        self.assertEqual(posting_date({'url': URL}, site='PTT', board='SampleBoard', aid=AID)[0], '2005/01/22')
        for url, board, aid in ((URL, 'OtherBoard', AID), (URL.replace('www.ptt.cc', 'example.test'), 'SampleBoard', AID), (URL, 'SampleBoard', '00000000')):
            self.assertEqual(posting_date({'url': url}, site='PTT', board=board, aid=aid)[0], '日期待查')
        self.assertEqual(posting_date({'url': URL, 'list_date':'1/23'}, site='PTT', board='SampleBoard', aid=AID)[0], '01/23（年份待查）')

    def test_forwarded_headers_do_not_supply_outer_article_date(self):
        post = {'author':'SampleUser', 'title':'Title'}
        header = '作者: SampleUser (nickname) 看板: SampleBoard\n標題: Title\n時間: Sat Jan 22 08:25:48 2005\n'
        self.assertEqual(posting_date(post, header, board='SampleBoard')[0], '2005/01/22')
        for body in ('※ [本文轉錄自 OtherBoard 看板]\n'+header, header.replace('SampleUser', 'OtherUser'), header.replace('SampleBoard', 'OtherBoard')):
            self.assertEqual(posting_date(post, body, board='SampleBoard')[0], '日期待查')
        self.assertEqual(posting_date(dict(post, date='Sat Jan 21 08:00:00 2006'), header, board='SampleBoard')[0], '2006/01/21')

    def test_export_uses_four_columns_and_preserves_data_and_body(self):
        with tempfile.TemporaryDirectory() as tmp, closing(app.database(Path(tmp))) as db:
            post = {'aid':AID, 'author':'SampleUser (PRIVATE_NICKNAME)', 'title':'<Title>', 'full_content':'original body', 'pass_format_check':True, 'list_date':'1/22'}
            app.save_article(db,'SampleBoard',post,site='PTT',source_index=42)
            before = db.execute('select body,data,fetched from articles').fetchone()
            board_metadata.save(db,'PTT','SampleBoard',{'board':'SampleBoard','title':'Board title','complete':True,'pins':[{'aid':AID,'title':'<Title>'}]})
            archive.export_html(db,Path(tmp))
            index=(Path(tmp)/'index.html').read_text(encoding='utf-8')
            self.assertIn('2005/01/22',index)
            self.assertIn('entry-number',index)
            self.assertIn('entry-date',index)
            self.assertIn('entry-author',index)
            self.assertIn('entry-title',index)
            self.assertIn('PRIVATE_NICKNAME',index)
            self.assertIn('&lt;Title&gt;',index)
            self.assertEqual(db.execute('select body,data,fetched from articles').fetchone(),before)
            # Both the list and search preserve the original author including nickname.
            self.assertIn('PRIVATE_NICKNAME',(Path(tmp)/'search-data.js').read_text(encoding='utf-8'))

    def test_new_form_order(self):
        html=Path('web.html').read_text(encoding='utf-8')
        section=html.split('<div class="form-row backup-fields">',1)[1].split('<div class="download-actions',1)[0]
        self.assertLess(section.index('name="folder"'),section.index('name="backup_mode"'))
        self.assertLess(section.index('name="backup_mode"'),section.index('name="refresh_board_info"'))


if __name__ == '__main__': unittest.main()
