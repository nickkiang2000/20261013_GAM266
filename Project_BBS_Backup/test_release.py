import json
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
import archive
from ansi_capture import CapturedLine, NumberedArticle
from scroll_capture import ScrollCapture
from test_scroll_capture import line, observe


class ReleaseTests(unittest.TestCase):
    def test_numbered_reader_continues_past_512(self):
        from board_session import BoardSession
        numbering=NumberedArticle(allow_wrapping=True,short_wrap_retry=True)
        session=BoardSession(SimpleNamespace(connect_core=SimpleNamespace(_decode_screen=Mock(),send=Mock()),cursor='>'))
        rows=[line('row '+str(i)) for i in range(620)]
        position=0
        def send(key):
            nonlocal position
            start=position
            frame=rows[start:start+23]
            end=start+23>=len(rows)
            numbering.observe(frame,f'瀏覽 第 1/3 頁 ({100 if end else 30}%) 第 {start+1}~{min(start+23,len(rows))} 行',at_end=end,styled=frame)
            position+=1
            return 0 if end else 1
        with patch.object(session,'_send_article_key',side_effect=send):
            self.assertEqual(session._read_numbered_article(lambda:numbering),'\n'.join(rows))
        self.assertGreater(position,512)

    def test_only_unadvertised_trailing_blank_can_be_repainted(self):
        rows=[line('a'),line('b'),line('c'),line('d'),line('e')]
        c=ScrollCapture()
        observe(c,[*rows[:4],line('')],1,4)
        observe(c,rows[1:],2,5,True)
        self.assertEqual([r[0] for r in c.result],list(rows))
        self.assertEqual(c.moves[-1]['padding_trims'],[1])
        c=ScrollCapture()
        observe(c,[*rows[:4],line('')],1,5)
        observe(c,rows[1:],2,5,True)
        self.assertIsNone(c.result)

    def test_stable_footer_offset_is_evidence_not_blanket_tolerance(self):
        for offset in (1,2):
            with self.subTest(offset=offset):
                rows=[line(str(i)) for i in range(9)]
                c=ScrollCapture()
                observe(c,rows[:5],1,5-offset)
                observe(c,rows[1:6],2,6-offset)
                observe(c,rows[2:7],3,7-offset)
                observe(c,rows[3:8],4,8-offset)
                observe(c,rows[4:],5,9-offset,True)
                self.assertEqual([r[0] for r in c.result],list(rows))
        c=ScrollCapture()
        rows=[line(str(i)) for i in range(8)]
        observe(c,rows[:4],1,4)
        observe(c,rows[1:5],2,5)
        observe(c,rows[2:6],3,6)
        observe(c,rows[3:],4,6,True)
        self.assertIsNone(c.result)

    def test_personalized_footer_advances_and_can_end_without_numbering(self):
        rows=[line(str(i)) for i in range(6)]
        n=NumberedArticle(allow_wrapping=True,short_wrap_retry=True)
        n.observe(rows[:4],'瀏覽 (20%) 第 1~4 行',at_end=False,styled=rows[:4])
        n.observe(rows[1:5],'瀏覽 (90%) ▲此頁內容會依閱讀者不同,原文未必有您的資料',at_end=False,styled=rows[1:5])
        self.assertEqual(n.unnumbered,0)
        n.observe(rows[2:],'瀏覽 (100%) ▲此頁內容會依閱讀者不同,原文未必有您的資料',at_end=True,styled=rows[2:])
        self.assertEqual(n.assemble(),'\n'.join(rows))
        self.assertTrue(n.personalized)
        self.assertTrue(n.scrolling.end_without_extent)

    def test_generic_missing_footer_is_not_personalized(self):
        n=NumberedArticle(allow_wrapping=True,short_wrap_retry=True)
        rows=[line(str(i)) for i in range(4)]
        n.observe(rows,'瀏覽 第 1~4 行',at_end=False,styled=rows)
        n.observe(rows,'unknown screen',at_end=True,styled=rows)
        self.assertIsNone(n.assemble())
        self.assertEqual(n.unnumbered,1)

    def test_marker_repaint_has_same_known_fragment(self):
        prefix='x'*78
        marker=CapturedLine(prefix+'\\',((prefix,('f1',)),('\\',('f7','hl'))))
        c=ScrollCapture()
        observe(c,[line('a'),line('b'),marker,line('tail')],1,3)
        observe(c,[line('b'),line(prefix+'\\',('f1',)),line('tail'),line('end')],2,4,True)
        self.assertIsNotNone(c.result)
        self.assertEqual(c.result[-2][0],prefix+'tail')

    def test_known_url_target_ignores_terminal_marker_not_normal_slash(self):
        full='https://example.com/a/long/resource/'
        label='https://example.com/a/long'
        html=archive.linkified_line(label+'\\',targets={label:full})
        self.assertIn('href="'+full+'"',html)
        normal=archive.linkified_line(full)
        self.assertIn('href="'+full+'"',normal)
        unknown=archive.linkified_line(label+'\\')
        self.assertNotIn('href="'+full+'"',unknown)

    def test_proven_split_url_becomes_one_complete_visible_anchor(self):
        from html.parser import HTMLParser
        target='https://example.com/long/article/path/'
        label='https://example.com/long/art'
        body=label+'\\\nicle/path/\nordinary text'
        page=archive.article_body(body,{},targets={label:target})
        class Links(HTMLParser):
            def __init__(self):
                super().__init__();self.links=[];self.active=None
            def handle_starttag(self,tag,attrs):
                if tag=='a':self.active=[dict(attrs)['href'],'']
            def handle_data(self,data):
                if self.active is not None:self.active[1]+=data
            def handle_endtag(self,tag):
                if tag=='a' and self.active is not None:self.links.append(self.active);self.active=None
        links=Links();links.feed(page)
        self.assertEqual(links.links,[[target,target]])
        self.assertIn('ordinary text',page)
        self.assertIn('class="soft-wrap-line"',page)
        self.assertIn('\\\n',body)
        unproven=archive.article_body(body,{},targets={label:'https://example.com/different/'})
        self.assertIn('icle/path/',unproven)
        self.assertNotIn('class="soft-wrap-line"',unproven)

    def test_wrapping_is_scoped_to_proven_lines_and_animation_is_excluded(self):
        body='甲'*40+'\n'+'乙'*120+'\n'+'丙'*40
        page=archive.article_body(body,{},soft_wrap_lines=[1])
        self.assertEqual(page.count('class="soft-wrap-line"'),1)
        self.assertIn('甲'*40+'\n<span',page)
        self.assertNotIn('white-space: pre-wrap',archive.ANSI_STYLE)
        animated=archive.color_article('PTT','SampleBoard','animation',{'soft_wrap_lines':[1]},'^Lmenu\n'+'乙'*120,'')
        self.assertNotIn('<span class="soft-wrap-line">',animated)

    def test_personalized_display_is_scoped_and_does_not_mutate_body(self):
        body='exampleuser 你有什麼特別的願望呢\nexampleuser is mentioned in ordinary text'
        page=archive.article_body(body,{},personalized=True)
        self.assertIn('〔讀者〕 你有什麼特別的願望呢',page)
        self.assertIn('exampleuser is mentioned',page)
        self.assertTrue(body.startswith('exampleuser'))

    def test_manual_replacement_requires_coverage_and_keeps_revision(self):
        old={'aid':'Sample01','full_content':'manual text','manual_ansi':{'original':'manual-ansi/example.ans'},'pass_format_check':True}
        new={'aid':'Sample01','full_content':'fresh text','pass_format_check':True,'ansi_diagnostics':{'reached_end':True},'ansi_coverage':{'matched':1,'total':1,'colored':0}}
        with tempfile.TemporaryDirectory() as directory, closing(app.database(Path(directory))) as db:
            app.save_article(db,'SampleBoard',old,site='PTT')
            bad=dict(new,ansi_coverage={'matched':0,'total':1})
            with patch('ansi_capture.read_colored_post',return_value=bad),patch.object(app,'STATE',{'saved':0,'skipped':0}):
                with self.assertRaises(app.ArticleReadError):
                    app.download_article(SimpleNamespace(),db,'PTT','SampleBoard','Sample01',existing=old)
            self.assertEqual(app.stored_article(db,'PTT','SampleBoard','Sample01')['full_content'],'manual text')
            with patch('ansi_capture.read_colored_post',return_value=dict(new)),patch.object(app,'STATE',{'saved':0,'skipped':0}):
                outcome,verified=app.download_article(SimpleNamespace(),db,'PTT','SampleBoard','Sample01',existing=old)
                self.assertEqual(outcome,'saved');self.assertTrue(verified)
                saved=app.stored_article(db,'PTT','SampleBoard','Sample01')
                self.assertNotIn('manual_ansi',saved)
                self.assertEqual(saved['replaces_manual'],old['manual_ansi'])
                self.assertEqual(json.loads(db.execute('select data from article_revisions').fetchone()[0]),old)
            bad=dict(new,ansi_coverage={'matched':0,'total':1})
            with patch('ansi_capture.read_colored_post',return_value=bad),patch.object(app,'STATE',{'saved':0,'skipped':0}):
                with self.assertRaises(app.ArticleReadError):
                    app.download_article(SimpleNamespace(),db,'PTT','SampleBoard','Sample01',existing=old)
            self.assertEqual(app.stored_article(db,'PTT','SampleBoard','Sample01')['full_content'],'fresh text')

    def test_manual_capture_requires_end_and_all_known_url_targets(self):
        post={'capture_version':app.CAPTURE_VERSION,'full_content':'text','ansi_coverage':{'matched':1,'total':1},'ansi_diagnostics':{'reached_end':True,'hyperlink_targets':['https://example.com/full/']}}
        self.assertFalse(app.manual_capture_ready(post))
        post['link_targets']={'https://example.com/short':'https://example.com/full/'}
        self.assertTrue(app.manual_capture_ready(post))
        post['ansi_diagnostics']['reached_end']=False
        self.assertFalse(app.manual_capture_ready(post))

    def test_manual_replacement_is_visible_in_archive(self):
        page=archive.color_article('PTT','SampleBoard','sample',{'replaces_manual':{'original':'manual-ansi/'+'a'*64+'.ans'}},'text','')
        self.assertIn('已由程式下載取代人工備份',page)
        self.assertIn('人工原檔</a>',page)

    def test_reading_time_limit_stops_without_partial_save(self):
        from board_session import BoardSession
        n=NumberedArticle(allow_wrapping=True,short_wrap_retry=True)
        session=BoardSession(SimpleNamespace(connect_core=SimpleNamespace(_decode_screen=Mock(),send=Mock()),cursor='>'))
        rows=[line('a'),line('b'),line('c')]
        def send(key):
            n.observe(rows,'瀏覽 (20%) 第 1~3 行',at_end=False,styled=rows)
            return 1
        with patch.object(session,'_send_article_key',side_effect=send),patch('board_session.time.monotonic',side_effect=[0,3000,3000]):
            self.assertIsNone(session._read_numbered_article(lambda:n))
        self.assertEqual(n.scrolling.error,'reading_time_limit')

    def test_first_failed_seam_is_in_saved_diagnostic_html(self):
        import local_state
        rows=[['accepted',[]],['next',[]]]
        diagnostic={'numbering':{'screen_overlap':{'first_failed_seam':{'accepted':rows,'observed':[['different',[]]]}}}}
        with tempfile.TemporaryDirectory() as directory,patch.object(local_state,'LOG_DIRECTORY',Path(directory)):
            sample={'screen':'article\nfooter','key':'down','height':24,'encoding':'utf-8','at_end':False,'linked_screen':False,'colors':[[]],'raw':b'article'}
            path=local_state.save_reading_capture('SampleBoard','Sample01','line',[sample],diagnostic,'test')
            html=path.read_text(encoding='utf-8')
            self.assertIn('最後確認成功的畫面',html)
            self.assertIn('第一次接合失敗的畫面',html)
            self.assertIn('different',html)

    def test_share_fixture_has_only_synthetic_metadata(self):
        rows=json.loads((Path(__file__).parent/'tests/fixtures/scroll_v223.json').read_text(encoding='utf-8'))
        self.assertTrue(all(r.get('synthetic') for r in rows))
        for r in rows:
            self.assertTrue(r['source'].startswith('synthetic-'))
            self.assertNotIn('http://',json.dumps(r))
            self.assertNotIn('https://',json.dumps(r))


if __name__=='__main__':unittest.main()
