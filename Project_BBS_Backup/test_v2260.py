import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
import board_metadata
import issue_records
from board_session import BoardSession, BoardNavigationError
from PyPtt import command
from scroll_capture import ScrollCapture, painted_key
from ansi_capture import CapturedLine, NumberedArticle
from manual_archive import prepare_ansi_article, store_ansi_article


class SparseSeamTests(unittest.TestCase):
    def capture(self):
        c=ScrollCapture();c.previous=[('',())]*6+[('only anchor',())]+[('',())]*16
        c.rows=c.previous[:];c.last_range=(5,27);c.revision=3
        return c
    def test_sparse_overlap_requires_established_history_and_equal_source_advance(self):
        c=self.capture();current=c.previous[6:]+[('new '+str(i),()) for i in range(6)]
        self.assertEqual(c.source_advance_seam(current,11,33),[(17,0)])
        c.revision=2;self.assertEqual(c.source_advance_seam(current,11,33),[])
        c.revision=3;self.assertEqual(c.source_advance_seam(current,11,34),[])
        self.assertEqual(c.source_advance_seam(current,4,26),[])
    def test_unchanged_full_screen_never_proves_advance(self):
        c=self.capture();self.assertEqual(c.source_advance_seam(c.previous,6,28),[])
    def test_source_advance_does_not_ignore_any_visible_content_or_background(self):
        c=self.capture();current=c.previous[6:]+[('new '+str(i),()) for i in range(6)]
        current[3]=(' ',((' ',('b1',)),))
        self.assertEqual(c.source_advance_seam(current,11,33),[])
    def test_only_foreground_on_plain_spaces_is_visually_equivalent(self):
        a=('  text',(('  ',('f1','hl')),('text',('f2',))))
        b=('  text',(('  ',()),('text',('f2',))))
        self.assertEqual(painted_key(a),painted_key(b))
        self.assertNotEqual(painted_key(('  text',(('  ',('b1',)),('text',('f2',))))),painted_key(b))
        self.assertNotEqual(painted_key(a),painted_key(('  text',(('  ',()),('text',('f3',))))))
    def test_numbered_color_conflict_on_visible_glyph_stays_unresolved(self):
        n=NumberedArticle()
        def observe(style):
            row=CapturedLine(' x',((' ',style),('x',('f2',))))
            n.observe([row],'瀏覽 第 1~1 行',at_end=True,styled=[row])
        observe(('f1',));observe(('f3','hl'))
        self.assertEqual(n.color_conflicts,0);self.assertEqual(n.space_color_equivalent_rows,{1})
        row=CapturedLine(' x',((' ',()),('x',('f3',))))
        n.observe([row],'瀏覽 第 1~1 行',at_end=True,styled=[row])
        self.assertGreater(n.color_conflicts,0)


class MetadataTests(unittest.TestCase):
    def snapshot(self):return {'board':'SampleBoard','title':'A < B','pins':[{'aid':'Pinned01','title':'first'},{'aid':'Pinned02','title':'second'}],'complete':True}
    def test_heading_and_sticky_symbols_do_not_confuse_m_mark_or_date(self):
        self.assertEqual(board_metadata.heading('【板主:SampleUser】 Trust your hand. 看板《SampleBoard》\nrows','SampleBoard'),'Trust your hand.')
        for row in ['> ★ m 4/23 SampleUser □ title','  * m 4/23 SampleUser □ title']:
            self.assertTrue(board_metadata.sticky_line(row))
        self.assertFalse(board_metadata.sticky_line('> 6882 m 4/23 SampleUser □ title'))
        with self.assertRaises(ValueError):board_metadata.heading('【板主:Someone】 wrong 看板《Other》','SampleBoard')
    def test_render_links_existing_aid_once_and_escapes_titles(self):
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            app.save_article(db,'SampleBoard',{'aid':'Pinned01','full_content':'body','pass_format_check':True},site='PTT2',source_index=10)
            board_metadata.save(db,'PTT2','SampleBoard',self.snapshot())
            rendered=board_metadata.render(db,'PTT2','SampleBoard')
            self.assertIn('A &lt; B',rendered);self.assertLess(rendered.index('first'),rendered.index('second'))
            self.assertEqual(rendered.count('href='),1);self.assertIn('正文尚未備份',rendered)
            self.assertEqual(db.execute('select count(*) from articles').fetchone()[0],1)
    def test_exported_board_contains_title_and_working_sticky_article_link(self):
        from html.parser import HTMLParser
        import archive
        class Pins(HTMLParser):
            def __init__(self):super().__init__();self.active=False;self.hrefs=[]
            def handle_starttag(self,tag,attrs):
                a=dict(attrs)
                if tag=='li' and a.get('class')=='sticky-entry':self.active=True
                if self.active and tag=='a':self.hrefs.append(a['href'])
            def handle_endtag(self,tag):
                if tag=='li':self.active=False
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            app.save_article(db,'SampleBoard',{'aid':'Pinned01','full_content':'body','pass_format_check':True},site='PTT2',source_index=10)
            board_metadata.save(db,'PTT2','SampleBoard',self.snapshot())
            archive.export_html(db,Path(tmp))
            page=(Path(tmp)/'index.html').read_text(encoding='utf-8')
            self.assertIn('A &lt; B',page)
            parsed=Pins();parsed.feed(page)
            self.assertEqual(len(parsed.hrefs),1)
            self.assertTrue((Path(tmp)/parsed.hrefs[0].split('#')[0]).is_file())

    def test_incomplete_or_duplicate_snapshot_cannot_replace_previous_state(self):
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            good=self.snapshot();board_metadata.save(db,'PTT2','SampleBoard',good)
            for bad in [dict(good,complete=False),dict(good,pins=[good['pins'][0]]*2)]:
                with self.assertRaises(ValueError):board_metadata.save(db,'PTT2','SampleBoard',bad)
            self.assertEqual(len(board_metadata.load(db,'PTT2','SampleBoard')['pins']),2)
    def session(self, end_on_number=False, duplicate=False):
        class FakeSession(BoardSession):
            def __init__(self):
                self.api=SimpleNamespace(cursor='>');self.check_stop=None;self.pos=0;self.board='SampleBoard';self.view='list'
                self.rows=['100 m 1/01 SampleUser □ ordinary','★ m 2/02 SampleUser □ first','★ m 3/03 SampleUser □ second']
            def _screen(self):return '【板主:SampleUser】 board title 看板《SampleBoard》\ncommands\ncolumns\n'+'\n'.join(('> ' if i==self.pos else '  ')+row for i,row in enumerate(self.rows))
            def _enter(self,board):
                if board!=self.board:raise BoardNavigationError()
            def _require_list(self,*args):self.view='list'
            def _return_to_list(self):self.view='list'
            def _checked_send(self,key,targets):
                if key=='$':self.pos=0 if end_on_number else 2
                elif key==command.up:self.pos=max(0,self.pos-1)
                elif key==command.down:self.pos=min(2,self.pos+1)
                else:raise AssertionError(key)
            def _query(self,key):
                self.view='query';return SimpleNamespace(aid='Pinned01' if duplicate or self.pos==1 else 'Pinned02',title='first' if self.pos==1 else 'second',author='SampleUser')
        return FakeSession()
    def test_pin_walk_preserves_order_for_both_end_cursor_conventions(self):
        for convention in [False,True]:
            s=self.session(end_on_number=convention)
            result=s.capture_board_metadata('SampleBoard')
            self.assertEqual([p['aid'] for p in result['pins']],['Pinned01','Pinned02'])
            self.assertEqual(s.view,'list')
    def test_refresh_reuses_existing_aid_and_new_pin_never_gets_date_as_index(self):
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            app.save_article(db,'SampleBoard',{'aid':'Pinned01','full_content':'old','pass_format_check':True},site='PTT2',source_index=10)
            session=Mock(capture_mode='page',view='list')
            session.capture_board_metadata.return_value=self.snapshot()
            session.selected=SimpleNamespace(aid='Pinned02',index=3)
            post={'aid':'Pinned02','index':3,'full_content':'new body','pass_format_check':True,'capture_version':app.CAPTURE_VERSION,'ansi_coverage':{'matched':1,'total':1},'ansi_diagnostics':{'reached_end':True}}
            with patch('ansi_capture.read_colored_post',return_value=post) as read,patch.object(app,'STATE',{'saved':0,'skipped':0}),patch.object(app.STOP,'is_set',return_value=False):
                app.refresh_board_info(session,db,'PTT2','SampleBoard',Mock())
                app.refresh_board_info(session,db,'PTT2','SampleBoard',Mock())
                self.assertEqual(read.call_count,1)
            self.assertIsNone(db.execute("select source_index from articles where aid='Pinned02'").fetchone()[0])
            self.assertEqual(db.execute("select source_index from articles where aid='Pinned01'").fetchone()[0],10)
            self.assertFalse(session.sticky_capture)

    def test_24_row_sticky_fallback_queries_aid_without_numeric_position(self):
        core=SimpleNamespace(_decode_screen=Mock(),send=Mock(),config=SimpleNamespace(screen_height=100))
        session=BoardSession(SimpleNamespace(connect_core=core,cursor='>'))
        selected=SimpleNamespace(aid='Pinned01',index=0)
        current=SimpleNamespace(aid='Pinned01',index=4,lock_post=False)
        with patch.object(session,'_return_to_list'),patch.object(session,'_resize_list'),patch.object(session,'_restore_height'),patch.object(session,'_position') as position,patch.object(session,'_query',return_value=current) as query,patch.object(session,'_read_numbered_article',return_value='complete body'):
            self.assertEqual(session._read_at_fallback_height(selected,lambda:None),'complete body')
        position.assert_not_called();self.assertIn('Pinned01',query.call_args.args[0])

    def test_pin_walk_rejects_duplicate_aid_instead_of_looping(self):
        with self.assertRaises(BoardNavigationError):self.session(duplicate=True).capture_board_metadata('SampleBoard')


class InventoryTests(unittest.TestCase):
    def test_color_and_failed_reread_are_deduplicated_and_all_colors_are_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with closing(app.database(root)) as db:
                app.save_article(db,'SampleBoard',{'aid':'Sample01','full_content':'body','pass_format_check':True},site='PTT2',source_index=1)
                issue_records.record(db,'PTT2','SampleBoard',1,'Sample01','讀取失敗')
                issue_records.record(db,'PTT2','SampleBoard',2,'Sample02','讀取失敗')
                db.executemany('insert into color_line_checks values(?,?,?,?,?,?)',[('PTT2','SampleBoard','Sample01',1,1,2),('PTT2','SampleBoard','Sample03',3,2,3)])
                db.commit()
            targets=app.repair_targets(root,'')
            self.assertEqual(len(targets),3)
            one=next(t for t in targets if t['index']==1)
            self.assertTrue(one['has_backup']);self.assertIn('待核色',one['status'])
            self.assertEqual(next(t for t in targets if t['index']==2)['status'],'缺正文')
    def test_manual_replacement_of_pending_color_preserves_original_history(self):
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            old={'aid':'Sample01','full_content':'old body','pass_format_check':True}
            app.save_article(db,'SampleBoard',old,site='PTT2',source_index=1)
            db.execute('insert into color_line_checks values(?,?,?,?,?,?)',('PTT2','SampleBoard','Sample01',1,1,2));db.commit()
            raw='作者  SampleUser  看板 SampleBoard\n標題  example\n正文'.encode()
            post=prepare_ansi_article(raw,'SampleBoard',site='PTT2',index=1,aid='Sample01')
            store_ansi_article(db,raw,post,Path(tmp),'PTT2','SampleBoard',1,'Sample01',save_article=app.save_article)
            self.assertEqual(json.loads(db.execute('select data from article_revisions').fetchone()[0]),old)
            self.assertEqual(db.execute('select count(*) from color_line_checks').fetchone()[0],0)
    def test_stale_index_issue_needs_newer_verified_log_evidence(self):
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            post={'aid':'Sample01','full_content':'body','pass_format_check':True,'ansi_coverage':{'matched':1,'total':1}}
            app.save_article(db,'SampleBoard',post,site='PTT2',source_index=1)
            issue_records.record(db,'PTT2','SampleBoard',1,None,'old network error');db.commit()
            last=db.execute('select last_seen from article_issues').fetchone()[0]
            issue_records.resolve_stale_index_issues(db,({},{}));self.assertEqual(db.execute('select count(*) from article_issues').fetchone()[0],1)
            issue_records.resolve_stale_index_issues(db,({('PTT2','SampleBoard',1,'Sample01'):last+1},{}))
            self.assertEqual(db.execute('select count(*) from article_issues').fetchone()[0],0)

if __name__=='__main__':unittest.main()
