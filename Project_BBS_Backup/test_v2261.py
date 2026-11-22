import json
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import Mock, patch
from board_session import BoardSession, ArticleStalled
import app
import archive
import board_metadata
from scroll_capture import ScrollCapture, edge_extension
from ansi_capture import CapturedLine


def row(text,style=()):return CapturedLine(text,((text,style),) if style else ())


class EdgeTests(unittest.TestCase):
    def setup_capture(self, tail=False):
        c=ScrollCapture();values=[row('row '+str(i)) for i in range(1,24)]
        values[10]=row('dot ' if tail else '.')
        c.rows=[(str(r),r.color_runs) for r in values];c.previous=c.rows[:];c.last_range=(1,23);c.revision=3
        return c,values
    def next_frame(self,first,tail=False):
        values=[row('row '+str(i)) for i in range(first,first+23)]
        values[11-first]=row('dot' if tail else '         .')
        return values
    def test_two_distinct_redraws_confirm_indentation_without_losing_intervening_rows(self):
        c,_=self.setup_capture();frame=self.next_frame(2)
        c.observe(frame,2,24,False)
        self.assertEqual(c.waiting,1);self.assertEqual(c.rows[10][0],'.')
        c.observe(self.next_frame(3),3,25,False)
        self.assertEqual(c.waiting,0);self.assertEqual(c.last_range,(3,25))
        self.assertEqual(c.rows[10][0],'         .');self.assertEqual(len(c.rows),25)
        self.assertEqual(c.rows[-2][0],'row 24');self.assertEqual(c.rows[-1][0],'row 25')
        self.assertEqual(c.first_failure['accepted'][10][0],'.')
        self.assertIn(11,c.edge_repaired_rows)
    def test_repeated_same_redraw_is_not_independent_evidence(self):
        c,_=self.setup_capture();frame=self.next_frame(2)
        for _ in range(3):c.observe(frame,2,24,False)
        self.assertEqual(c.rows[10][0],'.');self.assertEqual(c.revision,3)
    def test_top_redraw_preserves_previously_captured_row_and_uses_other_anchors(self):
        c,values=self.setup_capture();values[1]=row('.')
        c.rows=[(str(r),r.color_runs) for r in values];c.previous=c.rows[:]
        frame=[row('row '+str(i)) for i in range(2,25)]
        frame[0]=row('         .');frame[9]=row('.')
        c.observe(frame,2,24,False)
        self.assertEqual(c.waiting,0);self.assertEqual(c.rows[1][0],'.')
        self.assertIn(2,c.viewport_edge_reused_rows)
        self.assertEqual(c.rows[-1][0],'row 24')

    def test_trailing_plain_space_is_retained_instead_of_trimmed(self):
        c,_=self.setup_capture(tail=True)
        c.observe(self.next_frame(2,True),2,24,False);c.observe(self.next_frame(3,True),3,25,False)
        self.assertEqual(c.waiting,0);self.assertEqual(c.rows[10][0],'dot ')
    def test_background_or_changed_glyph_or_internal_spacing_is_not_repaired(self):
        a=('text ',(('text ',('b1',)),));b=('text',(('text',('b1',)),))
        self.assertIsNone(edge_extension(a,b))
        self.assertIsNone(edge_extension(('a  b',()),('a b',())))
        self.assertIsNone(edge_extension(('a',()),('  b',())))
        self.assertIsNone(edge_extension(('x',(('x',('f1',)),)),('  x',(('  x',('f2',)),))))


class ColorRetryTests(unittest.TestCase):
    def test_formal_page_color_conflict_returns_to_list_for_existing_retry_path(self):
        from ansi_capture import NumberedArticle
        n=NumberedArticle();rows=[row('a'),row('b'),row('c')]
        n.observe(rows,'瀏覽 第 1~3 行',at_end=True,styled=rows);n.color_conflicts=1
        session=BoardSession(SimpleNamespace(connect_core=SimpleNamespace(_decode_screen=Mock(),send=Mock()),cursor='>'))
        session.prefer_line_at_uncertain_seam=True
        with patch.object(session,'_send_article_key',return_value=0),patch.object(session,'_return_to_list') as leave:
            with self.assertRaisesRegex(ArticleStalled,'配色衝突'):session._read_article_pages(lambda:n)
        leave.assert_called_once()
        session.prefer_line_at_uncertain_seam=False
        with patch.object(session,'_send_article_key',return_value=0):
            self.assertEqual(session._read_article_pages(lambda:n),'a\nb\nc')


class LayoutTests(unittest.TestCase):
    def test_zero_explains_latest_and_parser_still_rejects_zero_in_a_list(self):
        self.assertEqual(app.parse_start_input('0'),0)
        self.assertIn('從最新文章開始',app.START_INPUT_ERROR)
        with self.assertRaises(ValueError):app.parse_start_input('0,10')
        html=Path('web.html').read_text(encoding='utf-8')
        self.assertIn(app.START_INPUT_ERROR,html)
        start=html.index('<div class="form-row backup-fields">');end=html.index('<div class="download-actions',start)
        section=html[start:end]
        self.assertLess(section.index('name="backup_mode"'),section.index('name="refresh_board_info"'))
        self.assertLess(section.index('name="folder"'),section.index('name="backup_mode"'))
    def test_pins_are_inside_only_latest_page_after_numbered_articles(self):
        class Pages(HTMLParser):
            def __init__(self):super().__init__();self.pages=[];self.active=None
            def handle_starttag(self,tag,attrs):
                a=dict(attrs)
                if tag=='ul' and a.get('class')=='entries':self.active=[];self.pages.append(self.active)
                if tag=='a' and self.active is not None:self.active.append(a)
            def handle_endtag(self,tag):
                if tag=='ul':self.active=None
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            for i in range(1,43):app.save_article(db,'SampleBoard',{'aid':f'Sample{i:02}','title':str(i),'full_content':'body','pass_format_check':True},site='PTT2',source_index=i)
            board_metadata.save(db,'PTT2','SampleBoard',{'board':'SampleBoard','title':'Bright centered title','complete':True,'pins':[{'aid':'Sample05','title':'first pin'},{'aid':'Sample15','title':'second pin'}]})
            archive.export_html(db,Path(tmp));page=(Path(tmp)/'index.html').read_text(encoding='utf-8')
            p=Pages();p.feed(page);self.assertEqual(len(p.pages),3)
            self.assertFalse(any(a.get('id','').startswith('pin-') for a in p.pages[0]+p.pages[1]))
            self.assertEqual([a.get('data-source-index') for a in p.pages[-1][:2]],['41','42'])
            self.assertTrue(all(a['id'].startswith('pin-') for a in p.pages[-1][2:]))
            self.assertIn('id="board-title"',page);self.assertIn('data-title="Bright centered title"',page)
            self.assertIn('position: sticky',page);self.assertIn('#ffff00',page)
            self.assertIn('board.title',archive.INDEX_KEYS)
            for a in p.pages[-1][2:]:
                self.assertTrue((Path(tmp)/a['href'].split('#')[0]).is_file())
                article=(Path(tmp)/a['href'].split('#')[0]).read_text(encoding='utf-8')
                self.assertIn("location.hash === '#sticky'",article)

if __name__=='__main__':unittest.main()
