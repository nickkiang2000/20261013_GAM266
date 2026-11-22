import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
from article_frame import ArticleFrame, BEGIN, END
from board_session import ArticleTargetUnit, BoardNavigationError, ArticleStalled
from ansi_capture import CapturedLine, NumberedArticle, read_colored_post
from PyPtt import screens, connect_core


class FrameTests(unittest.TestCase):
    def test_every_marker_split_and_repeated_redraw_is_tracked(self):
        for cut in range(1,len(BEGIN)):
            f=ArticleFrame();f.feed(BEGIN[:cut]);self.assertFalse(f.ready())
            f.feed(BEGIN[cut:]+b'body');self.assertFalse(f.ready())
            f.feed(END[:cut]);self.assertFalse(f.ready())
            f.feed(END[cut:]);self.assertTrue(f.ready())
        f=ArticleFrame();f.feed(BEGIN+b'first'+END+BEGIN+b'second')
        self.assertFalse(f.ready());f.feed(END);self.assertTrue(f.ready())

    def test_marker_free_legacy_response_does_not_require_new_protocol(self):
        f=ArticleFrame();f.feed(b'ordinary legacy screen\x1b[31mtext\x1b[m')
        self.assertTrue(f.ready());self.assertEqual(f.starts,0)

    def test_marker_free_article_still_requires_complete_footer_range(self):
        target=ArticleTargetUnit(screens.Target.InPost,ready=lambda:True)
        prefix='body\n瀏覽 第 18 頁 ( 4%) 目前顯示: 第 3'
        self.assertFalse(target.is_match(prefix,'>'))
        self.assertTrue(target.is_match(prefix+'67~389 行','>'))

    def test_closed_marker_followed_by_new_open_is_not_complete(self):
        f=ArticleFrame();f.feed(b'previous tail'+END+BEGIN+b'next partial')
        self.assertFalse(f.ready())

    def capture(self, complete=True, chunk_size=1):
        wire=BEGIN+b'\x1b[H\x1b[2Jalpha\nbeta\ngamma\ndelta\nepsilon\n'
        wire+='瀏覽 第 1/1 頁 (100%) 目前顯示: 第 1~5 行 (h)說明'.encode()
        if complete: wire+=END
        class Core:
            current_encoding='utf-8'
            config=SimpleNamespace(screen_height=6)
            def __init__(self): self.sent=[];self.parts=0
            def _stream_screen(self,encoding,data):
                parser=self._stream_parsers.setdefault(encoding,screens.IncrementalScreen(encoding,6))
                parser.feed(data);return parser.screen
            def send(self,key,targets):
                self.sent.append(key);self._stream_parsers={}
                for start in range(0,len(wire),chunk_size):
                    self.parts+=1;self.screen=self._stream_screen('utf-8',wire[start:start+chunk_size])
                    for i,t in enumerate(targets):
                        if t.is_match(self.screen,'>'):return i
                return -1
            def get_screen_queue(self):return [self.screen] if hasattr(self,'screen') else []
        core=Core()
        def get_post(board,aid):
            result=core.send('open',[connect_core.TargetUnit(screens.Target.PostEnd,break_detect=True)])
            if result<0:raise BoardNavigationError('incomplete redraw')
            return {'aid':aid,'full_content':'alpha\nbeta\ngamma\ndelta\nepsilon'}
        api=SimpleNamespace(connect_core=core,get_post=get_post)
        return core,wire,lambda:read_colored_post(api,'SampleBoard','Sample01')

    def test_real_capture_waits_for_last_chunk_including_split_utf8(self):
        for chunk in (1,2,7,4096):
            core,wire,run=self.capture(chunk_size=chunk)
            post=run()
            self.assertEqual(core.parts,(len(wire)+chunk-1)//chunk)
            self.assertEqual(core.sent,['open'])
            self.assertTrue(post['ansi_diagnostics']['reached_end'])
            self.assertEqual(post['full_content'],'alpha\nbeta\ngamma\ndelta\nepsilon')
            state=post['ansi_diagnostics']['frame_completion']['utf-8']
            self.assertFalse(state['in_progress'])
            if chunk==1:self.assertGreater(state['blocked_checks'],0)

    def test_unclosed_redraw_does_not_return_false_100_percent_success(self):
        core,_,run=self.capture(complete=False)
        with self.assertRaises(BoardNavigationError) as error:run()
        self.assertTrue(error.exception.capture_diagnostics['frame_completion']['utf-8']['in_progress'])
        self.assertFalse(error.exception.capture_diagnostics['reached_end'])
        self.assertEqual(core.sent,['open'])

    def test_gate_preserves_target_options_and_allows_list_after_completion(self):
        frame=ArticleFrame();frame.feed(BEGIN)
        old=connect_core.TargetUnit('list sentinel',break_detect=True,max_match=1)
        t=ArticleTargetUnit(old,ready=frame.ready)
        self.assertFalse(t.is_match('list sentinel','>'))
        self.assertEqual(t._current_match,0)
        frame.feed(END);self.assertTrue(t.is_match('list sentinel','>'))
        self.assertTrue(t.is_break());self.assertFalse(t.is_match('list sentinel','>'))


class VersionTests(unittest.TestCase):
    def post(self,body,matched,total):
        return {'aid':'Sample01','full_content':body,'title':'sample','pass_format_check':True,
                'capture_version':app.CAPTURE_VERSION,'ansi_coverage':{'matched':matched,'total':total},
                'ansi_diagnostics':{'reached_end':True}}

    def test_unverified_candidate_never_overwrites_existing_body_or_coverage(self):
        for oldmatched in (1,2):
            with self.subTest(oldmatched=oldmatched),tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
                old=self.post('old\nbody',oldmatched,2);new=self.post('different\nnew\nbody',2,3)
                app.save_article(db,'SampleBoard',old,site='PTT',source_index=3)
                with patch('ansi_capture.read_colored_post',return_value=new),patch.object(app,'STATE',{'saved':0,'skipped':0}):
                    outcome,verified=app.download_article(None,db,'PTT','SampleBoard','Sample01',existing=old,source_index=3)
                self.assertEqual(outcome,'kept_previous_color');self.assertFalse(verified)
                self.assertEqual(app.stored_article(db,'PTT','SampleBoard','Sample01')['full_content'],'old\nbody')
                role,data=db.execute('select role,data from article_revisions r join article_revision_roles t on t.revision_id=r.rowid').fetchone()
                self.assertEqual(role,'candidate');self.assertEqual(json.loads(data)['full_content'],'different\nnew\nbody')
                checks=db.execute('select matched,total from color_line_checks').fetchall()
                self.assertEqual(checks,[(1,2)] if oldmatched==1 else [])

    def test_verified_replacement_saves_previous_version_with_compatible_old_schema(self):
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            db.execute('create table article_revisions(site TEXT,board TEXT,aid TEXT,created REAL,data TEXT)')
            old=self.post('old',0,1);new=self.post('new',1,1)
            app.save_article(db,'SampleBoard',old,site='PTT')
            with patch('ansi_capture.read_colored_post',return_value=new),patch.object(app,'STATE',{'saved':0,'skipped':0}):
                self.assertEqual(app.download_article(None,db,'PTT','SampleBoard','Sample01',existing=old)[0],'saved')
            self.assertEqual(app.stored_article(db,'PTT','SampleBoard','Sample01')['full_content'],'new')
            role,data=db.execute('select role,data from article_revisions r join article_revision_roles t on t.revision_id=r.rowid').fetchone()
            self.assertEqual(role,'previous');self.assertEqual(json.loads(data),old)
            self.assertEqual(len(db.execute('pragma table_info(article_revisions)').fetchall()),5)

    def test_failed_replacement_rolls_back_history_and_body_together(self):
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            old=self.post('old',1,1);new=self.post('new',1,1)
            app.save_article(db,'SampleBoard',old,site='PTT')
            with patch('ansi_capture.read_colored_post',return_value=new),patch.object(app,'STATE',{'saved':0,'skipped':0}),patch.object(app,'save_article',side_effect=RuntimeError('disk failure')):
                with self.assertRaises(RuntimeError):app.download_article(None,db,'PTT','SampleBoard','Sample01',existing=old)
            self.assertEqual(app.stored_article(db,'PTT','SampleBoard','Sample01')['full_content'],'old')
            if db.execute("select count(*) from sqlite_master where name='article_revisions'").fetchone()[0]:
                self.assertEqual(db.execute('select count(*) from article_revisions').fetchone()[0],0)

    def test_safe_page_retry_uses_24_rows_even_without_initial_wrap_marker(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            api=Mock(capture_mode='page',view='list');api.get_post.return_value={'index':3,'aid':'Sample01'}
            observed=[]
            def read(api,board,aid):
                if api.capture_mode=='page':
                    exc=ArticleStalled(99);exc.capture_diagnostics={'numbering':{'wrap_marker_seen':False}};raise exc
                observed.append(api.wrapped_line_retry)
                return self.post('new',1,1)
            with patch('ansi_capture.read_colored_post',side_effect=read),patch.object(app,'STATE',{'saved':0,'skipped':0}),patch.object(app.STOP,'is_set',return_value=False):
                app.download_article(api,db,'PTT','SampleBoard','Sample01',source_index=3,checkpoint=True)
            self.assertEqual(observed,[True]);self.assertFalse(api.wrapped_line_retry)
            self.assertEqual(api.capture_mode,'page')


class ConflictCaptureTests(unittest.TestCase):
    def test_first_conflicting_color_pair_is_saved_even_when_body_assembles(self):
        import local_state
        from board_session import BoardSession
        wires=[b'\x1b[H\x1b[2Jalpha\n\x1b[31mbeta\x1b[m\ngamma\ndelta\n'+
               '瀏覽 第 1/2 頁 (50%) 目前顯示: 第 1~4 行'.encode(),
               b'\x1b[H\x1b[2J\x1b[32mbeta\x1b[m\ngamma\ndelta\nepsilon\n'+
               '瀏覽 第 2/2 頁 (100%) 目前顯示: 第 2~5 行'.encode()]
        class Core:
            current_encoding='utf-8'
            config=SimpleNamespace(screen_height=5)
            def __init__(self):self.wires=iter(wires)
            def _decode_screen(self,*args):return args[0]
            def _stream_screen(self,encoding,data):
                p=self._stream_parsers.setdefault(encoding,screens.IncrementalScreen(encoding,5))
                p.feed(data);return p.screen
            def send(self,key,targets):
                self._stream_parsers={};self.screen=self._stream_screen('utf-8',next(self.wires))
                return next(i for i,t in enumerate(targets) if t.is_match(self.screen,'>'))
            def get_screen_queue(self):return [self.screen] if hasattr(self,'screen') else []
        core=Core();session=BoardSession(SimpleNamespace(connect_core=core,cursor='>'))
        session.capture_mode='page'
        def get_post(*args,**kwargs):
            core.send('open',session._article_targets());core.send('page',session._article_targets())
            return {'aid':'Sample01','full_content':'alpha\nbeta\ngamma\ndelta\nepsilon'}
        with tempfile.TemporaryDirectory() as tmp,patch.object(session,'get_post',side_effect=get_post),patch.object(local_state,'LOG_DIRECTORY',Path(tmp)):
            post=read_colored_post(session,'SampleBoard','Sample01')
            self.assertTrue(post['ansi_diagnostics']['first_alignment_failure_saved'])
            self.assertEqual(post['ansi_coverage']['matched'],4)
            saved=json.loads(Path(post['ansi_diagnostics']['reading_capture']).with_suffix('.json').read_text(encoding='utf-8'))
            self.assertEqual(len(saved['frames']),2)
            colors=[f['colors'] for f in saved['frames']]
            self.assertNotEqual(colors[0][1],colors[1][0])


class WrappingTests(unittest.TestCase):
    def test_late_wrap_is_joined_after_long_numbered_prefix_without_inserting_blank_lines(self):
        n=NumberedArticle(allow_wrapping=True,short_wrap_retry=True)
        ordinary=[CapturedLine('row '+str(i),()) for i in range(2044)]
        prefix='x'*78
        marker=CapturedLine(prefix+'\\',((prefix,('f1',)),('\\',('f7','hl'))))
        tail=CapturedLine('tail',(('tail',('f1',)),))
        rows=ordinary+[marker,tail,CapturedLine('after A',()),CapturedLine('after B',())]
        for start in range(len(rows)-22):
            current=rows[start:start+23]
            # The two physical rows become one numbered source row.
            first=start+1-(start>2044)
            last=start+23-(start+23>2045)
            at_end=start+23==len(rows)
            n.observe(current,f'瀏覽 第 1/2 頁 ({100 if at_end else 40}%) 第 {first}~{last} 行',at_end=at_end,styled=current)
        self.assertIsNotNone(n.assemble())
        self.assertEqual(n.assemble().split('\n'),list(ordinary)+[prefix+'tail','after A','after B'])
        self.assertEqual(n.scrolling.wrapped_lines,[2044])
        self.assertEqual(n.colors[2045],((prefix,('f1',)),('tail',('f1',))))

if __name__=='__main__':unittest.main()
