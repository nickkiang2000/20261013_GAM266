import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from html.parser import HTMLParser
from PyPtt import command
import app, archive
from ansi_capture import CapturedLine, NumberedArticle
from board_session import BoardSession, END_KEY
from scroll_capture import ScrollCapture


def styled(rows):
    return [CapturedLine(text, runs) for text, runs in rows]


class SeamTests(unittest.TestCase):
    def test_empty_source_lines_advance_but_painted_or_repeated_art_does_not(self):
        for text, runs, permitted in [('', (), True), (' ', ((' ', ('b1',)),), False), ('art', (), False)]:
            c=ScrollCapture();c.previous=[(text,runs)]*23;c.rows=c.previous[:];c.last_range=(20,42);c.revision=5;c.offsets.extend([0,0])
            c.observe(styled(c.previous),21,43,False)
            self.assertEqual(c.error is None,permitted)
            if permitted:self.assertEqual(len(c.rows),24);self.assertEqual(c.blank_source_advances,1)
        c=ScrollCapture();c.previous=[('',())]*23;c.rows=c.previous[:];c.last_range=(20,42);c.revision=5
        self.assertEqual(c.source_advance_seam(c.previous,21,43),[])

    def test_painted_tail_is_preserved_after_two_distinct_matching_positions(self):
        original=[('row '+str(i),()) for i in range(1,24)]
        original[10]=('tail ',(('tail ',('f7','b1','hl')),))
        c=ScrollCapture();c.previous=original[:];c.rows=original[:];c.last_range=(1,23);c.revision=5
        for first in (2,3):
            current=original[first-1:]+[('row '+str(i),()) for i in range(24,first+23)]
            current[11-first]=('tail',(('tail',('f7','b1','hl')),))
            c.observe(styled(current),first,first+22,False)
            if first==2:self.assertGreater(c.waiting,0)
        self.assertIsNone(c.error);self.assertEqual(c.rows[10],original[10]);self.assertIn(11,c.confirmed_repaint_rows)

    def test_first_screen_blank_redraw_requires_two_positions_and_keeps_every_row(self):
        original=[('header '+str(i),()) for i in range(23)];original[17]=('',())
        c=ScrollCapture();c.previous=original[:];c.rows=original[:];c.last_range=(1,22);c.revision=1
        for first in (5,6):
            removed=first
            current=original[removed:]+[('new '+str(i),()) for i in range(removed)]
            current[17-removed]=('^L1',())
            c.observe(styled(current),first,first+22,False)
        self.assertIsNone(c.error);self.assertEqual(c.rows[17][0],'^L1');self.assertEqual(len(c.rows),29)
        self.assertIn(18,c.confirmed_repaint_rows)

    def test_repaint_never_substitutes_a_changed_glyph_or_explicit_glyph_color(self):
        for new in [('wrong',()),('row 10',(('row 10',('f3',)),))]:
            original=[('row '+str(i),()) for i in range(23)]
            c=ScrollCapture();c.rows=original[:];c.previous=original[:];c.last_range=(1,23);c.revision=5
            for first in (2,3,4):
                current=original[first-1:]+[('new '+str(i),()) for i in range(first-1)]
                current[11-first]=new
                c.observe(styled(current),first,first+22,False)
            self.assertIsNotNone(c.error);self.assertEqual(c.rows[10],original[10])


class EndAndRetryTests(unittest.TestCase):
    def session(self):
        return BoardSession(SimpleNamespace(connect_core=SimpleNamespace(_decode_screen=Mock(),send=Mock()),cursor='>'))

    def test_redraw_pacing_is_short_only_inside_verified_line_retry(self):
        from PyPtt import connect_core, screens
        for retry, expected in ((False,.95),(True,.05)):
            session=self.session();session.pacing=True;session.last_sent=10.0;session.refresh_each_line=retry
            with patch('board_session.time.monotonic',return_value=10.05),patch('board_session.time.sleep') as sleep:
                session._send(command.refresh,[connect_core.TargetUnit(screens.Target.PostEnd)])
            self.assertAlmostEqual(sleep.call_args.args[0],expected)

    def end_state(self):
        n=NumberedArticle();n.scrolling=ScrollCapture();c=n.scrolling
        c.rows=[('row '+str(i),()) for i in range(1,15)];c.previous=c.rows[-5:];c.last_range=(10,14);c.revision=8;c.offsets.extend([0,0])
        n._observe_scrolling(styled(c.previous),10,14,False)
        return n

    def test_83_percent_requires_identical_colored_end_up_end(self):
        for failure in (None,'no_up','changed_color','changed_extent','not_last_page'):
            with self.subTest(failure=failure):
                s=self.session();n=self.end_state();original=n.scrolling.previous[:]
                state={'range':(10,14),'frame':original[:],'page':'1/2' if failure=='not_last_page' else '1/1'}
                s._screen=lambda:f'瀏覽 第 {state["page"]} 頁 (83%) 目前顯示: 第 {state["range"][0]}~{state["range"][1]} 行'
                calls=[]
                def send(key):
                    self.assertTrue(s.probing_scroll_end);calls.append(key)
                    state['range']=(9,13) if key==command.up and failure!='no_up' else (10,14)
                    state['frame']=original[:]
                    if key==END_KEY and len(calls)==3:
                        if failure=='changed_color':state['frame'][0]=('row 10',(('row 10',('f1',)),))
                        if failure=='changed_extent':state['range']=(11,15)
                    s.last_article_frame=state['frame'];return 1
                s._send_article_key=send
                body=s._verify_scroll_end(lambda:n)
                if failure is None:
                    self.assertEqual(body,'\n'.join('row '+str(i) for i in range(1,15)))
                    self.assertEqual(calls,[END_KEY,command.up,END_KEY]);self.assertEqual(n.end_evidence,'colored_end_up_end_round_trip')
                else:self.assertIsNone(body)
                self.assertFalse(getattr(s,'probing_scroll_end',False))

    def test_redraw_retry_requeries_same_aid_once_and_restores_flag(self):
        for wrong in (False,True):
            s=self.session();n=self.end_state();selected=SimpleNamespace(aid='Sample01',lock_post=False);s.selected=selected
            s._return_to_list=Mock();s._query=Mock(return_value=SimpleNamespace(aid='Other001' if wrong else 'Sample01',lock_post=False))
            states=[]
            def read(get_numbering):
                states.append(getattr(s,'refresh_each_line',False));return None if len(states)==1 else 'body'
            s._read_numbered_pass=read
            self.assertEqual(s._read_numbered_article(lambda:n),None if wrong else 'body')
            self.assertEqual(states,[False] if wrong else [False,True]);self.assertFalse(getattr(s,'refresh_each_line',False))
            s._query.assert_called_once_with('#Sample01'+command.enter)
            self.assertEqual(s.operations['full_redraw_retries'],0 if wrong else 1)

    def test_failed_full_redraw_retry_is_bounded_and_keeps_diagnostic(self):
        s=self.session();n=self.end_state();selected=SimpleNamespace(aid='Sample01',lock_post=False);s.selected=selected
        s._return_to_list=Mock();s._query=Mock(return_value=selected)
        with patch.object(s,'_read_numbered_pass',return_value=None) as read:
            self.assertIsNone(s._read_numbered_article(lambda:n));self.assertEqual(read.call_count,2)
        self.assertIsNotNone(s.full_redraw_initial_failure);self.assertFalse(s.refresh_each_line)


class CaptureIntegrationTests(unittest.TestCase):
    def capture(self, *, redraw=False, anomalous=False):
        from ansi_capture import ColorScreen, read_colored_post
        core=SimpleNamespace(config=SimpleNamespace(screen_height=24),current_encoding='utf-8',
                             _stream_parsers={},_decode_screen=Mock(),last_timeout_was_silent=False)
        queue=[];position=0;keys=[];count=28 if anomalous else 24
        def stream(encoding,wire):
            parser=ColorScreen(24);lines=parser.replay(wire,encoding)
            core._stream_parsers[encoding]=parser
            queue[:]=['\n'.join(lines)]
        core._stream_screen=stream;core.get_screen_queue=lambda:queue
        def transport(key,targets,*args,**kwargs):
            nonlocal position
            keys.append(key)
            if key==command.enter*2:position=0
            elif key==command.down:position=min(count-23,position+1)
            elif key==command.up:position=max(0,position-1)
            elif key==END_KEY:position=count-23
            lines=['row '+str(i) for i in range(position+1,position+24)]
            if redraw and key!=command.refresh:lines[0]='BAD partial redraw'
            percent=83 if anomalous else (100 if position==count-23 else 30)
            footer=f'瀏覽 第 1/1 頁 ({percent}%) 目前顯示: 第 {position+1}~{position+23} 行 (h)說明'
            wire=('\x1b[2J\x1b[H'+'\r\n'.join(lines+[footer])).encode('utf-8')
            core._stream_screen('utf-8',wire)
            return 0 if percent==100 else 1
        core.send=transport
        class Session(BoardSession):
            def get_post(self,board,*,aid,get_numbering,fallback_attempts):
                self.refresh_each_line=redraw
                body=self._read_numbered_pass(get_numbering)
                return {'aid':aid,'full_content':body,'pass_format_check':True}
        session=Session(SimpleNamespace(connect_core=core,cursor='>'))
        session.view='article'
        with tempfile.TemporaryDirectory() as tmp,patch('local_state.LOG_DIRECTORY',Path(tmp)):
            post=read_colored_post(session,'SampleBoard','Sample01')
        return post,keys

    def test_full_redraw_capture_ignores_partial_down_frames(self):
        post,keys=self.capture(redraw=True)
        self.assertEqual(post['full_content'],'\n'.join('row '+str(i) for i in range(1,25)))
        self.assertNotIn('BAD',post['full_content'])
        self.assertEqual(post['ansi_coverage']['matched'],24)
        self.assertEqual(keys,[command.enter*2,command.refresh,command.down,command.refresh])

    def test_83_percent_round_trip_captures_full_body_without_backwards_seam(self):
        post,keys=self.capture(anomalous=True)
        self.assertEqual(post['full_content'],'\n'.join('row '+str(i) for i in range(1,29)))
        self.assertEqual(post['ansi_coverage']['matched'],28)
        self.assertEqual(keys[-3:],[END_KEY,command.up,END_KEY])
        self.assertEqual(post['ansi_diagnostics']['numbering']['end_evidence'],'colored_end_up_end_round_trip')


class LayoutTests(unittest.TestCase):
    def test_two_fixed_rows_put_search_before_oldest_and_keep_nickname(self):
        class Parser(HTMLParser):
            def __init__(self):super().__init__();self.header=0;self.tools=0;self.in_header=False;self.in_tools=False;self.search_in_header=False;self.order=[]
            def handle_starttag(self,tag,attrs):
                a=dict(attrs)
                if tag=='header' and a.get('class')=='index-top':self.header+=1;self.in_header=True
                if tag=='div' and a.get('class')=='board-tools':self.tools+=1
                if tag=='button' and a.get('class') in ('open-search','oldest','older','newer','latest'):self.order.append(a['class']);self.search_in_header |= self.in_header
            def handle_endtag(self,tag):
                if tag=='header':self.in_header=False
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            app.save_article(db,'SampleBoard',{'aid':'Sample01','author':'SampleUser (Nickname)','title':'title','full_content':'text','pass_format_check':True},site='PTT2',source_index=1)
            archive.export_html(db,Path(tmp));html=(Path(tmp)/'index.html').read_text(encoding='utf-8');p=Parser();p.feed(html)
            self.assertEqual((p.header,p.tools),(1,1));self.assertFalse(p.search_in_header)
            self.assertEqual(p.order,['open-search','oldest','older','newer','latest'])
            self.assertIn('SampleUser (Nickname)',html);self.assertNotIn('日期（年／月／日）',html)
            self.assertIn('height: 30px',html);self.assertIn('flex-wrap: nowrap',html)


if __name__=='__main__':unittest.main()
