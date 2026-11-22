import json
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PyPtt import command, screens
import app, archive, board_metadata, issue_records
from ansi_capture import ColorScreen, normalize_reserved_cells, read_colored_post
from article_frame import BlankScreenControls
from article_metadata import list_cells, push_cell
from board_session import BoardSession, END_KEY


class TerminalCompatibilityTests(unittest.TestCase):
    def test_reserved_pairs_across_sgr_keep_real_spaces_glyphs_and_color(self):
        wire=b'\x1b[H\x1b[31mA  \x1b[44m\x08\x08B  \r\nend'
        frame=ColorScreen(24).replay(wire,'ascii')
        self.assertEqual(str(frame[0]),'AB  ')
        self.assertEqual(frame[0].styles[0],('f1',))
        self.assertEqual(frame[0].styles[1],('f1','b4'))
        self.assertEqual('\n'.join(frame),screens.VT100Parser(wire,'ascii',24).screen)
        self.assertEqual(normalize_reserved_cells('x\x08'),'x\x08')
        self.assertEqual(normalize_reserved_cells(' \x1b[2;1H\x08'),' \x1b[2;1H\x08')
        self.assertEqual(normalize_reserved_cells('   \x08\x08'),' ')

    def test_blank_reverse_index_requires_clear_and_top_margin_at_every_chunk_boundary(self):
        wire=b'\x1b[?2026h\x1b[H\x1b[2J\x1b[24;1H\x1b[J\x1b[H\x1bM\x1b[Htext\x1b[?2026l'
        expected=wire.replace(b'\x1bM',b'')
        for split in range(len(wire)+1):
            control=BlankScreenControls()
            self.assertEqual(control.feed(wire[:split])+control.feed(wire[split:]),expected)
            self.assertEqual(control.removed,1)
        for unsafe in (b'\x1bM',b'\x1b[H\x1b[2Jx\x1bM',b'\x1b[H\x1b[2J\x1b[2;1H\x1bM'):
            control=BlankScreenControls();self.assertEqual(control.feed(unsafe),unsafe);self.assertEqual(control.removed,0)

    def test_real_sdk_and_colored_reader_complete_reverse_index_end_probe(self):
        core=SimpleNamespace(config=SimpleNamespace(screen_height=24),current_encoding='utf-8',
                             _stream_parsers={},_decode_screen=Mock(),last_timeout_was_silent=False)
        queue=[];position=0;keys=[]
        def stream(encoding,wire):
            parser=core._stream_parsers.get(encoding)
            if parser is None: parser=core._stream_parsers[encoding]=screens.IncrementalScreen(encoding,24)
            parser.feed(wire);queue[:]=[parser.screen]
        core._stream_screen=stream;core.get_screen_queue=lambda:queue
        def transport(key,targets,*args,**kwargs):
            nonlocal position
            keys.append(key)
            if key==command.enter*2:position=0
            elif key==command.down:position=min(5,position+1)
            elif key==command.up:position=max(0,position-1)
            elif key==END_KEY:position=5
            prefix='\x1b[H\x1b[2J'
            if key==command.up:prefix+='\x1b[24;1H\x1b[J\x1b[H\x1bM\x1b[H'
            lines=['row '+str(i) for i in range(position+1,position+24)]
            footer=f'瀏覽 第 {2 if position==5 else 1}/2 頁 (83%) 目前顯示: 第 {position+1}~{position+23} 行 (h)說明'
            wire=(prefix+'\r\n'.join(lines+[footer])).encode()
            # The adapter must handle control sequences split between packets.
            split=wire.find(b'\x1bM')+1 if key==command.up else len(wire)//2
            core._stream_parsers.clear()
            core._stream_screen('utf-8',wire[:split]);core._stream_screen('utf-8',wire[split:])
            return 1 if '瀏覽' in queue[-1] else -1
        core.send=transport
        class Session(BoardSession):
            def get_post(self,board,*,aid,get_numbering,fallback_attempts):
                return {'aid':aid,'full_content':self._read_numbered_pass(get_numbering),'pass_format_check':True}
        session=Session(SimpleNamespace(connect_core=core,cursor='>'));session.view='article'
        with tempfile.TemporaryDirectory() as tmp,patch('local_state.LOG_DIRECTORY',Path(tmp)):
            post=read_colored_post(session,'SampleBoard','Sample01')
        self.assertEqual(post['full_content'],'\n'.join('row '+str(i) for i in range(1,29)))
        self.assertEqual(post['ansi_coverage']['matched'],28)
        self.assertEqual(post['ansi_diagnostics']['empty_screen_reverse_indices_removed'],1)
        self.assertEqual(post['ansi_diagnostics']['numbering']['end_evidence'],'colored_end_up_end_round_trip')
        self.assertEqual(keys[-3:],[END_KEY,command.up,END_KEY])


class ArchiveInterfaceTests(unittest.TestCase):
    def test_push_labels_preserve_station_values_instead_of_counting_comments(self):
        for value in ('1','25','爆','X1','XX'):
            html=push_cell({'push_number':value,'comments':[{'type':'ARROW'}]*50})
            self.assertIn('>'+value+'</span>',html)
        self.assertNotIn('50',push_cell({'comments':[{'type':'PUSH'}]*50}))
        self.assertIn('未保存站方推文數',push_cell({}))
        self.assertNotIn('&lt;script',push_cell({'push_number':'<script>'}))

    def test_first_topic_mark_and_reply_title_do_not_duplicate(self):
        post={'author':'SampleUser (a very long nickname)','push_number':'2'}
        html=list_cells(1,post,'<title>')
        self.assertIn('topic-mark',html);self.assertIn('□',html);self.assertIn('&lt;title&gt;',html)
        self.assertIn('title="SampleUser (a very long nickname)"',html)
        for title in ('Re: title','re : title','R: title'):
            self.assertNotIn('topic-mark',list_cells(2,post,title))

    def test_report_groups_boards_outside_six_column_table_and_preserves_links(self):
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            app.save_article(db,'One',{'aid':'Sample01','full_content':'body','pass_format_check':True},site='PTT',source_index=1)
            issue_records.record(db,'PTT','One',1,'Sample01','first reason')
            issue_records.record(db,'PTT2','Two',2,'Sample02','second <reason>')
            html=issue_records.report(db)
            self.assertIn('PTT / One 錯漏文章',html);self.assertIn('PTT2 / Two 錯漏文章',html)
            self.assertEqual(html.count('<table '),2);self.assertEqual(html.count('<th scope="col">'),12)
            self.assertNotIn('<th>站台',html);self.assertNotIn('<td>PTT / One',html)
            self.assertIn('查看已存<br>文章',html);self.assertIn('尚無可讀<br>存檔',html)
            self.assertIn('&lt;reason&gt;',html)

    def test_index_title_and_spacing_are_updated_without_changing_stored_post(self):
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            post={'aid':'Sample01','title':'title','author':'SampleUser (Nickname)','push_number':'3','full_content':'body','pass_format_check':True}
            app.save_article(db,'SampleBoard',post,site='PTT',source_index=1)
            original=db.execute('select body,data from articles').fetchone()
            archive.export_html(db,Path(tmp));html=(Path(tmp)/'index.html').read_text(encoding='utf-8')
            self.assertIn('<title>BBS時光庫 PTT / SampleBoard</title>',html)
            self.assertIn('min(37.5%, 58ch)',html);self.assertIn('text-overflow: ellipsis',html)
            self.assertIn('.sticky-star { color: #ffff00; text-align: center; }',html)
            self.assertEqual(db.execute('select body,data from articles').fetchone(),original)
            self.assertIn('更新標題<br>／置底文',Path('web.html').read_text(encoding='utf-8'))

    def test_board_title_snapshot_survives_a_later_article_failure(self):
        import PyPtt
        with tempfile.TemporaryDirectory() as tmp,patch.object(PyPtt,'API') as factory,patch.object(app,'BoardSession',side_effect=lambda api:api),patch.object(app,'STATE',{'saved':0,'skipped':0,'unresolved':0}),patch.object(app.STOP,'is_set',return_value=False),patch('local_state.LOG_DIRECTORY',Path(tmp)/'logs'),patch('ansi_capture.read_colored_post',side_effect=app.ArticleReadError('article failed')):
            api=factory.return_value;api.operations={}
            api.capture_board_metadata.return_value={'board':'SampleBoard','title':'Saved title','pins':[],'complete':True}
            app.work(dict(site='PTT',board='SampleBoard',aid='Sample01',folder=tmp,user='sample',password='dummy',refresh_board_info=True))
            self.assertEqual(app.STATE['result'],'pending')
            with closing(app.database(Path(tmp))) as db:
                self.assertEqual(board_metadata.load(db,'PTT','SampleBoard')['title'],'Saved title')
                self.assertEqual(db.execute('select count(*) from articles').fetchone()[0],0)
            html=(Path(tmp)/'index.html').read_text(encoding='utf-8')
            # A failed first-ever article can leave no index rows; metadata is still persisted.
            self.assertTrue(html)


if __name__=='__main__':unittest.main()
