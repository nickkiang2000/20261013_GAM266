"""Synthetic CCNS protocol, numbered-screen and application regression tests."""
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import unittest

import app
import archive
import ccns_session as c


def frame(lines, footer='', *, offset=0, color='0;37', clear=True):
    value='\x1b[2J\x1b[H' if clear else ''
    for y,line in enumerate(lines,offset+1):
        value+=f'\x1b[{y};1H\x1b[{color}m\x1b[K'+line
    value+='\x1b[24;1H\x1b[0m\x1b[K'+footer
    return value.encode('big5uao')


def article():
    return ['作者 SampleUser (Nickname) 站內 B.A.95','標題 [測試] synthetic','時間 Tue Dec 16 21:36:31 2008']+['text '+str(n) for n in range(4,29)]


def footer(page,pages,first,last,percent):
    return f'瀏覽 第 {page}/{pages} 頁 ({percent}%) 目前顯示：第 {first:02d}~{last:02d} 行 (h)按鍵說明 ←[q]離開'


class FakeSocket:
    def __init__(self):self.sent=[]
    def settimeout(self,timeout):pass
    def sendall(self,data):self.sent.append(data)
    def close(self):pass


class ProtocolTests(unittest.TestCase):
    def test_telnet_fragmentation_and_literal_iac(self):
        sock=FakeSocket();wire=c.TelnetWire(sock);result=b''
        for b in b'a\xff\xfd\x1f\xff\xfd\x18\xff\xfa\x18\x01\xff\xf0b\xff\xff':
            result+=wire.feed(bytes([b]))
        self.assertEqual(result,b'ab\xff')
        self.assertIn(b'\xff\xfb\x1f',sock.sent)
        self.assertIn(b'\xff\xfa\x1f\x00\x50\x00\x18\xff\xf0',sock.sent)
        self.assertIn(b'\xff\xfa\x18\x00VT100\xff\xf0',sock.sent)
        wire.write(b'a\xffb');self.assertEqual(sock.sent[-1],b'a\xff\xffb')

    def test_split_big5_and_sgr_are_not_replacement_text(self):
        term=c.Terminal();raw='中'.encode('big5uao')
        for data in (raw[:1],b'\x1b[1;33m',raw[1:]):term.feed(data)
        self.assertTrue(term.ready);self.assertTrue(term.text.startswith('中'))
        self.assertEqual(term.rows()[0],[['中',['f3','hl']]])

    def test_big5_symbols_keep_two_terminal_cells(self):
        term=c.Terminal();term.feed('◇★→'.encode('big5uao'))
        self.assertEqual(term.cursor.x,6)
        term.feed(b'\x1b[1;7HX');self.assertTrue(term.text.startswith('◇★→X'))

    def test_cursor_redraw_clear_and_color(self):
        term=c.Terminal();term.feed(frame(['abc'],color='1;33;44'))
        term.feed(b'\x1b[1;2H\x1b[1;33;44mX\x1b[K')
        self.assertTrue(term.rows()[0][0][0].startswith('aX'));self.assertEqual(term.rows()[0][0][1],['f3','b4','hl'])

    def test_screens_require_board_and_cursor(self):
        text='【板主:Sample】 title 看板《B.A.95》\n[→]閱讀\n編號 日 期 作 者\n> 4013 03/25 Sample ◇ title\n文章選讀'
        self.assertTrue(c.is_list(text,'B.A.95'));self.assertFalse(c.is_list(text,'Other'))
        self.assertEqual(c.selected_row(text)[0],4013)
        self.assertFalse(c.is_main(text))
        self.assertTrue(c.is_main('【主功能表】\n(B)oards 【 佈告討論區 】'))


class ArticleTests(unittest.TestCase):
    def test_two_pages_overlap_and_color_export(self):
        term=c.Terminal();pages=c.NumberedPages();lines=article()
        term.feed(frame(lines[:23],footer(1,2,1,23,82)));pages.add(term)
        term.feed(frame(lines[22:],footer(2,2,23,28,100)));pages.add(term)
        post=pages.post('Sample01',4000,'B.A.95')
        self.assertEqual(post['full_content'],'\n'.join(lines))
        self.assertEqual(post['ansi_coverage'],dict(matched=28,total=28))
        self.assertEqual(post['author'],'SampleUser (Nickname)')
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            app.save_article(db,'B.A.95',post,site='CCNS',source_index=4000)
            archive.export_html(db,Path(tmp))
            self.assertIn('BBS時光庫 CCNS / B.A.95',(Path(tmp)/'index.html').read_text(encoding='utf-8'))
            self.assertEqual(db.execute('select body from articles').fetchone()[0],post['full_content'])

    def test_gap_missing_first_page_and_color_conflict_are_rejected(self):
        for kind in ('gap','first','color','text'):
            with self.subTest(kind=kind):
                term=c.Terminal();pages=c.NumberedPages();lines=article()
                if kind!='first':
                    term.feed(frame(lines[:23],footer(1,2,1,23,82)));pages.add(term)
                body=lines[22:]
                if kind=='text':body=['changed']+body[1:]
                term.feed(frame(body,footer(2,2,24 if kind=='gap' else 23,29 if kind=='gap' else 28,100),color='1;33' if kind=='color' else '0;37'))
                with self.assertRaises(c.CCNSError):pages.add(term)

    def test_no_100_percent_no_complete_capture(self):
        term=c.Terminal();pages=c.NumberedPages()
        term.feed(frame(article()[:23],footer(1,1,1,23,83)));pages.add(term)
        with self.assertRaises(c.CCNSError):pages.post('Sample01',1,'B.A.95')

    def test_colored_empty_lines_are_preserved(self):
        term=c.Terminal();lines=article()[:5]+['']
        term.feed(frame(lines,footer(1,1,1,6,100)))
        term.feed(b'\x1b[6;1H\x1b[44m  ')
        pages=c.NumberedPages();pages.add(term)
        post=pages.post('Sample01',1,'B.A.95')
        self.assertTrue(post['full_content'].endswith('  '));self.assertIn('b4',post['ansi_lines']['5'][0][1])

    def test_terminal_bright_backslash_without_continuation_is_preserved(self):
        term=c.Terminal();term.feed(frame(article()[:3],footer(1,1,1,3,100)))
        term.feed(b'\x1b[3;80H\x1b[1;37m\\')
        pages=c.NumberedPages();pages.add(term)
        self.assertTrue(pages.post('Sample01',1,'B.A.95')['full_content'].endswith('\\'))

    def test_header_must_match_board(self):
        term=c.Terminal();pages=c.NumberedPages();term.feed(frame(article()[:3],footer(1,1,1,3,100)));pages.add(term)
        with self.assertRaises(c.CCNSError):pages.post('Sample01',1,'Other')


class ScriptedSession(c.CCNSSession):
    def __init__(self):
        super().__init__();self.sent=[];self.board='B.A.95';self.view='list';self.show_list()
    def show_list(self):
        self.terminal.feed(frame(['【板主:Sample】 title 看板《B.A.95》','[→]閱讀','編號 日 期 作 者','> 30 03/25 Sample ◇ title'],'文章選讀'))
    def login(self,*args,**kwargs):pass
    def get_newest_index(self,*args,**kwargs):return 30
    def _command(self,keys,predicate,**kwargs):
        self.sent.append(keys)
        keys=keys.removesuffix('\x0c')
        if keys in ('#Sample01\r','30\r',' ' ,'\x1b[D'):self.show_list()
        elif keys=='q':self.terminal.feed(frame(['文章代碼及資訊查詢：','文章代碼：#Sample01'],'請按任意鍵繼續'))
        elif keys=='\x1b[C':self.terminal.feed(frame(article()[:23],footer(1,2,1,23,82)))
        elif keys=='\x1b[6~':self.terminal.feed(frame(article()[22:],footer(2,2,23,28,100)))
        else:raise AssertionError(repr(keys))
        if not predicate(self.terminal.text):raise AssertionError('unexpected screen')
        return self.terminal.text


class SessionTests(unittest.TestCase):
    def test_query_read_return_and_identity(self):
        session=ScriptedSession();post=session.read_colored_post('B.A.95','Sample01')
        self.assertEqual(post['index'],30);self.assertTrue(session.confirm_list('B.A.95'))
        self.assertEqual(session.sent.count('q'),2)

    def test_download_pipeline_saves_and_rechecks(self):
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db,patch.object(app,'STATE',dict(saved=0,skipped=0)):
            session=ScriptedSession();session.capture_mode='ccns_page'
            outcome,verified=app.download_article(session,db,'CCNS','B.A.95','Sample01',source_index=30,checkpoint=True)
            self.assertEqual(outcome,'saved');self.assertTrue(verified)
            saved=app.stored_article(db,'CCNS','B.A.95','Sample01')
            self.assertTrue(app.complete_capture(saved))
            outcome,_=app.download_article(session,db,'CCNS','B.A.95','Sample01',source_index=30,existing=saved,checkpoint=True)
            self.assertEqual(outcome,'unchanged')
            self.assertEqual(app.resume_checkpoints(db,'CCNS','B.A.95')[30],('Sample01','saved'))

    def test_board_validation_is_station_specific(self):
        self.assertTrue(app.valid_board('CCNS','B.A.95'));self.assertFalse(app.valid_board('PTT','B.A.95'))
        for board in ('../bad','a/b','B.A.95\r','', 'x'*31):self.assertFalse(app.valid_board('CCNS',board))

    def test_failure_capture_excludes_login_and_keeps_article(self):
        session=ScriptedSession();original=session._command
        def command(keys,predicate,**kwargs):
            result=original(keys,predicate,**kwargs)
            if keys=='\x1b[6~':session.terminal.feed(b'\x1b[1;1Hwrong')
            return result
        session._command=command
        with patch('ccns_session.save_reading_capture',return_value=None) as capture:
            with self.assertRaises(c.CCNSError):session.read_colored_post('B.A.95','Sample01')
            self.assertEqual(capture.call_count,1)
            self.assertTrue(all(c.page_extent(item['screen']) for item in capture.call_args.args[3]))



class BatchSession(c.CCNSSession):
    fail_index = None
    def __init__(self,**kwargs):
        super().__init__(**kwargs);self.board='B.A.95';self.view='list';self.reads=[]
    def login(self,*args,**kwargs):pass
    def confirm_list(self,board):return board==self.board
    def recover_list_after_read_error(self,board):return self.confirm_list(board)
    def get_newest_index(self,*args,**kwargs):return 30
    def get_post(self,board,*,index=None,aid=None,**kwargs):
        index=index if index is not None else int(aid[1:])
        return dict(aid=f'C{index:07d}',index=index,is_lock=False)
    def read_colored_post(self,board,aid):
        index=int(aid[1:]);self.reads.append(index)
        if index==self.fail_index:raise c.CCNSError('synthetic seam failure')
        term=c.Terminal();pages=c.NumberedPages()
        term.feed(frame(article()[:3],footer(1,1,1,3,100)));pages.add(term)
        return pages.post(aid,index,board)


class BatchTests(unittest.TestCase):
    def test_thirty_articles_failure_then_resume_without_touching_other_boards(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(app,'STATE',dict(saved=0,skipped=0,unresolved=0,timed_samples=0,ansi_review=0,ansi_samples=0,ansi_colored=0,resumed_verified=0,issues=[])),patch.object(app.STOP,'wait',return_value=False),patch.object(app.STOP,'is_set',return_value=False),patch('local_state.LOG_DIRECTORY',Path(tmp)/'logs'),patch('ccns_session.CCNSSession',BatchSession),patch('PyPtt.API') as ptt:
            root=Path(tmp)/'CCNS_B.A.95'
            config=dict(site='CCNS',board='B.A.95',folder=str(root),user='SyntheticUser',password='SyntheticSecret',mode='new',new_batch=True,start=0,count=30,direction='newest',backup_mode='fill',refresh_board_info=True)
            with patch.object(BatchSession,'fail_index',17):app.work(dict(config))
            ptt.assert_not_called()
            self.assertEqual(app.STATE['saved'],29);self.assertEqual(app.STATE['scanned'],30)
            self.assertEqual(app.STATE['unresolved'],1);self.assertEqual(app.STATE['result'],'pending')
            with closing(app.database(root)) as db:
                self.assertEqual(db.execute('select count(*) from articles').fetchone()[0],29)
                self.assertEqual(db.execute('select first_index,last_index,completed from batch_ranges').fetchone(),(1,30,0))
            app.STATE.update(saved=0,skipped=0,unresolved=0,timed_samples=0,ansi_review=0,issues=[])
            app.work(dict(config,mode='resume',new_batch=False))
            self.assertEqual(app.STATE['result'],'done');self.assertEqual(app.STATE['saved'],1)
            with closing(app.database(root)) as db:
                self.assertEqual(db.execute('select count(*) from articles').fetchone()[0],30)
                self.assertEqual(db.execute('select completed from batch_ranges').fetchone()[0],1)
                self.assertEqual(db.execute('select count(*) from article_issues').fetchone()[0],0)
            logs=''.join(p.read_text(encoding='utf-8') for p in (Path(tmp)/'logs').glob('*.log'))
            self.assertNotIn('SyntheticSecret',logs);self.assertNotIn('SyntheticUser',logs)
            self.assertIn('"terminal_height": 24',logs)

    def test_stop_retains_pending_batch_for_resume(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(app,'STATE',dict(saved=0,skipped=0,unresolved=0,timed_samples=0,ansi_review=0,ansi_samples=0,ansi_colored=0,resumed_verified=0,issues=[])),patch.object(app.STOP,'wait',return_value=False),patch('local_state.LOG_DIRECTORY',Path(tmp)/'logs'),patch('ccns_session.CCNSSession',BatchSession):
            checks=iter([False,False,False,True])
            with patch.object(app.STOP,'is_set',side_effect=lambda:next(checks,True)):
                app.work(dict(site='CCNS',board='B.A.95',folder=tmp,user='SyntheticUser',password='SyntheticSecret',mode='new',new_batch=True,start=0,count=30,direction='newest',backup_mode='fill'))
            self.assertEqual(app.STATE['result'],'stopped')
            with closing(app.database(Path(tmp))) as db:
                self.assertEqual(db.execute('select completed from batch_ranges').fetchone()[0],0)
                self.assertLess(db.execute('select count(*) from articles').fetchone()[0],30)


class LoginTests(unittest.TestCase):
    def test_announcements_are_recognized_instead_of_four_blind_keys(self):
        class LoginSession(c.CCNSSession):
            screens=[]
            def __init__(self,events):super().__init__(connector=lambda *a,**k:FakeSocket(),event=lambda name,**fields:events.append((name,fields)));self.sent=[]
            def _wait(self,predicate,**kwargs):return '請輸入代號：'
            def _command(self,keys,predicate,**kwargs):
                self.sent.append(keys);text=self.screens.pop(0)
                self.assertion=predicate(text)
                if not self.assertion:raise AssertionError('unexpected login state')
                return text
        for count in (0,2,4,6):
            events=[];session=LoginSession(events)
            session.screens=['請輸入密碼：']+['請按任意鍵繼續']*count+['【主功能表】\n(B)oards 【 佈告討論區 】']
            session.login('SyntheticUser','SyntheticSecret')
            self.assertEqual(session.sent.count(' '),count)
            self.assertEqual(session.view,'main');self.assertEqual(events[-1][1]['announcements'],count)
            self.assertNotIn('SyntheticSecret',repr(events))

    def test_unknown_post_login_prompt_never_sends_dismiss_key(self):
        class LoginSession(c.CCNSSession):
            def __init__(self):super().__init__(connector=lambda *a,**k:FakeSocket());self.sent=[]
            def _wait(self,*args,**kwargs):return '請輸入代號：'
            def _command(self,keys,predicate,**kwargs):self.sent.append(keys);return '密碼：' if len(self.sent)==1 else '重複登入，是否刪除其他連線？'
        session=LoginSession()
        with self.assertRaises(c.CCNSError):session.login('SyntheticUser','SyntheticSecret')
        self.assertEqual(len(session.sent),2)




class LoginNavigationRegressionTests(unittest.TestCase):
    def test_default_boards_selection_needs_no_new_redraw(self):
        class MenuSession(c.CCNSSession):
            def __init__(self,selected=True):
                super().__init__();self.view='main';self.sent=[]
                self.terminal.feed(frame(['【主功能表】','  > (B)oards    【 佈告討論區 】' if selected else '    (B)oards    【 佈告討論區 】']))
            def _command(self,keys,predicate,**kwargs):
                self.sent.append(keys)
                if keys=='b':self.terminal.feed(frame(['【主功能表】','  > (B)oards    【 佈告討論區 】']))
                elif keys=='\r':self.terminal.feed(frame(['看板目錄']))
                elif keys=='s':self.terminal.feed(frame(['請輸入看板名稱：']))
                elif keys=='B.A.95\r':self.terminal.feed(frame(['【板主:Sample】 title 看板《B.A.95》','[→]閱讀','編號 日 期 作 者','> 30 03/25 Sample ◇ title'],'文章選讀'))
                else:raise AssertionError(repr(keys))
                if not predicate(self.terminal.text):raise AssertionError('unexpected menu state')
                return self.terminal.text
        for selected in (True,False):
            session=MenuSession(selected);session._enter('B.A.95')
            self.assertTrue(session.confirm_list('B.A.95'))
            self.assertEqual('b' in session.sent,not selected)
            self.assertEqual(session.sent[-3:],['\r','s','B.A.95\r'])

    def test_exact_duplicate_login_prompt_preserves_existing_sessions(self):
        prompt='偵測到多重登入，您想刪除其他重複的 login (Y/n) 嗎？[Y]'
        self.assertTrue(c.duplicate_login_prompt(prompt))
        self.assertFalse(c.duplicate_login_prompt('重複登入，是否刪除其他連線？'))
        class LoginSession(c.CCNSSession):
            def __init__(self):super().__init__(connector=lambda *a,**k:FakeSocket());self.sent=[];self.screens=['密碼：',prompt,'請按任意鍵繼續','【主功能表】\n(B)oards 【 佈告討論區 】']
            def _wait(self,*args,**kwargs):return '請輸入代號：'
            def _command(self,keys,predicate,**kwargs):
                self.sent.append(keys);text=self.screens.pop(0)
                if not predicate(text):raise AssertionError('unexpected login state')
                return text
        session=LoginSession();session.login('SyntheticUser','SyntheticSecret')
        self.assertEqual(session.sent,['SyntheticUser\r','SyntheticSecret\r','n\r',' '])
        self.assertEqual(session.view,'main')

    def test_duplicate_prompt_is_not_answered_repeatedly(self):
        prompt='偵測到多重登入，您想刪除其他重複的 login (Y/n) 嗎？[Y]'
        class LoginSession(c.CCNSSession):
            def __init__(self):super().__init__(connector=lambda *a,**k:FakeSocket());self.sent=[]
            def _wait(self,*args,**kwargs):return '請輸入代號：'
            def _command(self,keys,predicate,**kwargs):self.sent.append(keys);return '密碼：' if len(self.sent)==1 else prompt
        session=LoginSession()
        with self.assertRaises(c.CCNSError):session.login('SyntheticUser','SyntheticSecret')
        self.assertEqual(session.sent.count('n\r'),1)
        self.assertNotIn('y\r',session.sent)




class ListRefreshRegressionTests(unittest.TestCase):
    def test_end_page_down_and_same_index_require_explicit_redraw(self):
        class ListSession(ScriptedSession):
            get_newest_index=c.CCNSSession.get_newest_index
            def _command(self,keys,predicate,**kwargs):
                if keys.startswith(('\x1b[4~','\x1b[6~','30\r','#Sample01\r')):
                    self.assertion=keys.endswith('\x0c')
                    if not self.assertion:raise AssertionError('no-op positioning would produce no reply')
                    self.sent.append(keys);self.show_list()
                    if not predicate(self.terminal.text):raise AssertionError('unverified list')
                    return self.terminal.text
                return super()._command(keys,predicate,**kwargs)
        session=ListSession()
        self.assertEqual(session.get_newest_index(board='B.A.95'),30)
        self.assertEqual(session.get_post('B.A.95',index=30,query=True)['aid'],'Sample01')
        self.assertEqual(session.get_post('B.A.95',aid='Sample01',query=True)['index'],30)
        self.assertIn('\x1b[4~\x0c',session.sent)
        self.assertIn('\x1b[6~\x0c',session.sent)
        self.assertIn('30\r\x0c',session.sent)
        self.assertIn('#Sample01\r\x0c',session.sent)
        self.assertIn('q',session.sent)
        self.assertNotIn('q\x0c',session.sent)

    def test_list_refresh_never_runs_from_unverified_view(self):
        session=ScriptedSession();session.view='article'
        with self.assertRaises(c.CCNSError):session._list_command('30\r','B.A.95')
        self.assertEqual(session.sent,[])




class QueryPermissionRegressionTests(unittest.TestCase):
    def test_permission_notice_is_not_mistaken_for_missing_reply(self):
        class DeniedSession(ScriptedSession):
            def _command(self,keys,predicate,**kwargs):
                if keys=='q':
                    self.sent.append(keys)
                    self.terminal.feed(frame(['★ 您的權限不足'],'[請按任意鍵繼續]'))
                    if not predicate(self.terminal.text):raise AssertionError('permission notice not recognized')
                    return self.terminal.text
                return super()._command(keys,predicate,**kwargs)
        session=DeniedSession()
        with self.assertRaisesRegex(c.CCNSError,'權限不足'):session.get_post('B.A.95',index=30,query=True)
        self.assertTrue(session.confirm_list('B.A.95'))
        self.assertNotIn('\x1b[C',session.sent)




class IndentedHeaderRegressionTests(unittest.TestCase):
    def test_header_padding_is_accepted_without_changing_text_or_colors(self):
        for padding in (' ', '  ', '\t'):
            with self.subTest(padding=repr(padding)):
                term=c.Terminal();pages=c.NumberedPages()
                lines=[padding+line for line in article()[:3]]
                term.feed(frame(lines,footer(1,1,1,3,100),color='0;37;44'))
                pages.add(term)
                original={n:[[value,list(style)] for value,style in runs] for n,runs in pages.lines.items()}
                original_body='\n'.join(''.join(value for value,_ in original[n]) for n in range(1,4))
                post=pages.post('Sample01',30,'B.A.95')
                self.assertEqual(post['author'],'SampleUser (Nickname)')
                self.assertEqual(post['title'],'[測試] synthetic')
                self.assertEqual(post['date'],'Tue Dec 16 21:36:31 2008')
                self.assertEqual(post['full_content'],original_body)
                self.assertTrue(post['full_content'].startswith(' '))
                self.assertEqual(post['ansi_lines'],{str(n-1):runs for n,runs in pages.lines.items()})
                self.assertEqual(post['ansi_coverage'],dict(matched=3,total=3))
                self.assertEqual(pages.lines,original)

    def test_indented_header_still_requires_matching_board(self):
        term=c.Terminal();pages=c.NumberedPages()
        term.feed(frame([' '+line for line in article()[:3]],footer(1,1,1,3,100)))
        pages.add(term)
        with self.assertRaises(c.CCNSError):pages.post('Sample01',30,'Other')




class StickyAndForwardedTests(unittest.TestCase):
    def test_star_number_requires_consistent_numeric_neighbors(self):
        head='【板主:Sample】 title 看板《B.A.95》\n[→]閱讀\n編號 日 期 作 者\n'
        text=head+'  28 03/23 Sample ◇ before\n> ★ 04/08 Sample ⇒ title\n  30 04/11 Sample ◇ after\n文章選讀'
        self.assertEqual(c.selected_row(text)[0],29);self.assertTrue(c.selected_sticky(text))
        for broken in (text.replace('  30 ','  31 '),text.replace('  28 03/23 Sample ◇ before\n','')):
            with self.assertRaises(c.CCNSError):c.selected_row(broken)

    def test_forwarded_header_requires_independent_identity_verification(self):
        term=c.Terminal();pages=c.NumberedPages()
        lines=article()[:3];lines[0]=lines[0].replace('B.A.95','SourceBoard')
        term.feed(frame(lines,footer(1,1,1,3,100)));pages.add(term)
        with self.assertRaises(c.CCNSError):pages.post('Sample01',29,'B.A.95')
        post=pages.post('Sample01',29,'B.A.95',identity_verified=True)
        self.assertEqual(post['board'],'B.A.95');self.assertEqual(post['original_header_board'],'SourceBoard')
        self.assertIn('SourceBoard',post['full_content'])

    def test_confirmed_bright_wrap_is_joined_and_authored_slash_preserved(self):
        lines=article()[:3]+['x'*77+'\\','continued','tail']
        term=c.Terminal();term.feed(frame(lines,footer(1,1,1,5,100)))
        term.feed(b'\x1b[4;78H\x1b[1;37m\\')
        pages=c.NumberedPages();pages.add(term)
        post=pages.post('Sample01',29,'B.A.95')
        self.assertEqual(post['full_content'].splitlines()[3],'x'*77+'continued')
        self.assertEqual(post['soft_wrap_lines'],[3])
        self.assertEqual(post['ansi_coverage'],dict(matched=5,total=5))
        ordinary=c.Terminal();ordinary.feed(frame(article()[:3]+['ordinary\\'],footer(1,1,1,4,100)))
        plain=c.NumberedPages();plain.add(ordinary)
        self.assertTrue(plain.post('Sample01',29,'B.A.95')['full_content'].endswith('ordinary\\'))

    def test_sticky_archive_entry_only_on_latest_page(self):
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db:
            for index in (1,30):
                app.save_article(db,'B.A.95',dict(aid=f'S{index:07d}',title='Synthetic title',author='SampleUser',date='2026-11-23',full_content='body',pass_format_check=True,ccns_sticky=index==1),site='CCNS',source_index=index)
            original=db.execute('select body,data from articles order by source_index').fetchall()
            archive.export_html(db,Path(tmp))
            html=(Path(tmp)/'index.html').read_text(encoding='utf-8')
            pages=html.split('<ul class="entries"')[1:]
            self.assertEqual(len(pages),2)
            self.assertNotIn('class="sticky-entry"',pages[0].split('</ul>',1)[0])
            self.assertIn('class="sticky-entry"',pages[1].split('</ul>',1)[0])
            self.assertIn('sticky-star',pages[1])
            self.assertEqual(db.execute('select body,data from articles order by source_index').fetchall(),original)




class OldArticleEndingTests(unittest.TestCase):
    def test_zero_start_footer_is_normalized_and_checked_against_visible_lines(self):
        term=c.Terminal();pages=c.NumberedPages()
        term.feed(frame(article()[:15],footer(1,1,0,15,100)));pages.add(term)
        post=pages.post('Sample01',2,'B.A.95')
        self.assertEqual(post['ansi_coverage'],dict(matched=15,total=15))
        self.assertEqual(post['ansi_diagnostics']['final_extent']['reported_first'],0)
        self.assertEqual(post['full_content'],'\n'.join(article()[:15]))
        self.assertIsNone(c.page_extent('\n'*23+footer(2,2,0,15,100)))

    def test_colored_signature_terminal_slash_preserves_all_spaces_and_colors(self):
        term=c.Terminal();lines=article()[:3]+['body','signature'+' '*68+'\\']
        term.feed(frame(lines,footer(1,1,1,5,100)))
        term.feed(b'\x1b[5;1H\x1b[32;44m'+b'signature'+b' '*68+b'\x1b[0;1;37m\\')
        before=term.rows()[4]
        pages=c.NumberedPages();pages.add(term);post=pages.post('Sample01',2,'B.A.95')
        self.assertTrue(post['full_content'].endswith('signature'+' '*68+'\\'))
        self.assertEqual(post['ansi_lines']['4'],before)
        self.assertEqual(post['soft_wrap_lines'],[])

    def test_final_100_percent_may_keep_same_last_line(self):
        class OldSession(ScriptedSession):
            def __init__(self):super().__init__();self.pages=0
            def _command(self,keys,predicate,**kwargs):
                lines=article()[:25]
                if keys=='\x1b[C':
                    self.terminal.feed(frame(lines[:23],footer(1,2,1,23,78)))
                elif keys=='\x1b[6~':
                    self.pages+=1
                    self.terminal.feed(frame(lines[2:] if self.pages==1 else lines[3:],footer(1,2,3,25,99) if self.pages==1 else footer(2,2,4,25,100)))
                else:return super()._command(keys,predicate,**kwargs)
                self.sent.append(keys)
                if not predicate(self.terminal.text):raise AssertionError('invalid old footer')
                return self.terminal.text
        session=OldSession();post=session.read_colored_post('B.A.95','Sample01')
        self.assertEqual(session.pages,2)
        self.assertEqual(post['ansi_coverage'],dict(matched=25,total=25))
        self.assertEqual(post['full_content'],'\n'.join(article()[:25]))




class QueryRecoveryTests(unittest.TestCase):
    def test_query_panel_without_supported_aid_returns_to_same_list(self):
        class MissingCode(ScriptedSession):
            def _command(self,keys,predicate,**kwargs):
                if keys=='q':
                    self.sent.append(keys)
                    self.terminal.feed(frame(['文章代碼及資訊查詢：','文章代碼：#SHORT'],'請按任意鍵繼續'))
                    if not predicate(self.terminal.text):raise AssertionError('query completion should be recognized')
                    return self.terminal.text
                return super()._command(keys,predicate,**kwargs)
        session=MissingCode()
        with self.assertRaises(c.CCNSQueryUnavailable) as caught:session.get_post('B.A.95',index=30,query=True)
        self.assertTrue(session.confirm_list('B.A.95'))
        self.assertEqual(caught.exception.capture_diagnostics['code_length'],5)
        self.assertEqual(caught.exception.capture_diagnostics['observed_code'],'SHORT')
        self.assertNotIn('\x1b[C',session.sent)

    def test_timeout_query_notice_can_recover_with_one_acknowledgement(self):
        class TimeoutNotice(ScriptedSession):
            def _command(self,keys,predicate,**kwargs):
                if keys=='q':
                    self.sent.append(keys);self.terminal.feed(frame(['文章代碼及資訊查詢：'],'請按任意鍵繼續'))
                    raise c.CCNSError('synthetic query timeout')
                return super()._command(keys,predicate,**kwargs)
        session=TimeoutNotice()
        with self.assertRaises(c.CCNSError):session.get_post('B.A.95',index=30,query=True)
        self.assertEqual(session.view,'query_pending')
        self.assertTrue(session.recover_list_after_read_error('B.A.95'))
        self.assertEqual(session.sent.count(' '),1)

    def test_unknown_confirmation_or_wrong_board_is_never_dismissed_blindly(self):
        session=ScriptedSession();session.view='query_pending'
        session.terminal.feed(frame(['是否刪除？ (Y/n)']))
        self.assertFalse(session.recover_list_after_read_error('B.A.95'))
        self.assertEqual(session.sent,[])
        self.assertFalse(session.recover_list_after_read_error('Other'))
        self.assertFalse(c.query_continue('\n'*23+'請按任意鍵 (Y/n)'))

    def test_stale_list_requires_a_fresh_redraw_for_recovery(self):
        class RefreshSession(ScriptedSession):
            def _command(self,keys,predicate,**kwargs):
                if keys=='\x0c':
                    self.sent.append(keys);self.show_list();return self.terminal.text
                return super()._command(keys,predicate,**kwargs)
        session=RefreshSession();session.view='query_pending'
        self.assertTrue(session.recover_list_after_read_error('B.A.95'))
        self.assertEqual(session.sent,['\x0c'])

    def test_query_failure_skips_single_article_and_resume_can_repair_it(self):
        class QueryFailure(BatchSession):
            broken=True
            def get_post(self,board,*,index=None,aid=None,**kwargs):
                if index==17 and self.broken:raise c.CCNSQueryUnavailable('synthetic unsupported article code')
                return super().get_post(board,index=index,aid=aid,**kwargs)
        with tempfile.TemporaryDirectory() as tmp,patch.object(app,'STATE',dict(saved=0,skipped=0,unresolved=0,timed_samples=0,ansi_review=0,ansi_samples=0,ansi_colored=0,resumed_verified=0,issues=[])),patch.object(app.STOP,'wait',return_value=False),patch.object(app.STOP,'is_set',return_value=False),patch('local_state.LOG_DIRECTORY',Path(tmp)/'logs'),patch('ccns_session.CCNSSession',QueryFailure):
            root=Path(tmp)/'CCNS_B.A.95'
            config=dict(site='CCNS',board='B.A.95',folder=str(root),user='SyntheticUser',password='SyntheticSecret',mode='new',new_batch=True,start=0,count=30,direction='newest',backup_mode='fill')
            app.work(dict(config))
            self.assertEqual(app.STATE['result'],'pending');self.assertEqual(app.STATE['scanned'],30)
            self.assertEqual(app.STATE['saved'],29);self.assertEqual(app.STATE['unresolved'],1)
            with closing(app.database(root)) as db:
                self.assertEqual(db.execute('select idx from article_issues').fetchall(),[(17,)])
            QueryFailure.broken=False
            app.STATE.update(saved=0,skipped=0,unresolved=0,timed_samples=0,ansi_review=0,issues=[])
            app.work(dict(config,mode='resume',new_batch=False))
            self.assertEqual(app.STATE['result'],'done');self.assertEqual(app.STATE['saved'],1)
            with closing(app.database(root)) as db:self.assertEqual(db.execute('select count(*) from article_issues').fetchone()[0],0)

    def test_real_connection_close_is_not_treated_as_a_skippable_query(self):
        class Closed(BatchSession):
            def get_post(self,board,*,index=None,aid=None,**kwargs):
                if index==29:raise c.ConnectionClosed('synthetic socket close')
                return super().get_post(board,index=index,aid=aid,**kwargs)
        with tempfile.TemporaryDirectory() as tmp,patch.object(app,'STATE',dict(saved=0,skipped=0,unresolved=0,timed_samples=0,ansi_review=0,ansi_samples=0,ansi_colored=0,resumed_verified=0,issues=[])),patch.object(app.STOP,'wait',return_value=False),patch.object(app.STOP,'is_set',return_value=False),patch('local_state.LOG_DIRECTORY',Path(tmp)/'logs'),patch('ccns_session.CCNSSession',Closed):
            app.work(dict(site='CCNS',board='B.A.95',folder=tmp,user='SyntheticUser',password='SyntheticSecret',mode='new',new_batch=True,start=0,count=30,direction='newest',backup_mode='fill'))
            self.assertEqual(app.STATE['result'],'disconnected');self.assertEqual(app.STATE['scanned'],1)
            self.assertEqual(app.STATE['saved'],1)
            with closing(app.database(Path(tmp))) as db:self.assertEqual(db.execute('select completed from batch_ranges').fetchone()[0],0)




class QueryCodeBoundaryTests(unittest.TestCase):
    def test_partial_code_is_never_accepted_as_a_valid_aid(self):
        for code in ('SHORT','Sample012','Sample01.extra'):
            self.assertIsNone(c.AID.search('文章代碼：#'+code+'  '))
        self.assertEqual(c.AID.search('文章代碼：#Sample01  ')[1],'Sample01')

if __name__=='__main__':unittest.main()


class EMarkedSession(ScriptedSession):
    query_notice_text='文章被鎖定、編輯或者刪除中'
    marker='e04'
    list_title='title'
    def __init__(self):
        self.opens=0
        super().__init__()
    def show_list(self):
        self.terminal.feed(frame(['【板主:Sample】 title 看板《B.A.95》','[→]閱讀','編號 日 期 作 者',f'> 30 {self.marker} 03/25 SampleUser ◇ {self.list_title}'],'文章選讀'))
    def _command(self,keys,predicate,**kwargs):
        if keys=='q':
            self.sent.append(keys)
            self.terminal.feed(frame(['文章代碼及資訊查詢：',self.query_notice_text],'請按任意鍵繼續'))
            if not predicate(self.terminal.text):raise AssertionError('notice not recognized')
            return self.terminal.text
        if keys=='\x1b[C':self.opens+=1
        return super()._command(keys,predicate,**kwargs)


class EMarkedArticleTests(unittest.TestCase):
    def test_no_aid_article_saved_exported_and_resume_identity_rechecked(self):
        session=EMarkedSession()
        query=session.get_post('B.A.95',index=30,query=True)
        key=query['aid']
        self.assertTrue(key.startswith('CCNS-E-30-'))
        self.assertFalse(query['is_lock'])
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db,patch.object(app,'STATE',dict(saved=0,skipped=0)):
            outcome,verified=app.download_article(session,db,'CCNS','B.A.95',key,source_index=30,checkpoint=True)
            self.assertEqual((outcome,verified),('saved',True))
            saved=app.stored_article(db,'CCNS','B.A.95',key)
            self.assertIsNone(saved['source_aid'])
            self.assertEqual(saved['identity_kind'],'ccns_e_without_aid')
            self.assertEqual(session.opens,2)
            self.assertFalse(any(command.startswith('#CCNS-E') for command in session.sent))
            self.assertEqual(app.resume_checkpoints(db,'CCNS','B.A.95')[30],(key,'saved'))
            archive.export_html(db,Path(tmp))
            self.assertTrue((Path(tmp)/'index.html').exists())
            self.assertEqual(session.get_post('B.A.95',index=30,query=True)['aid'],key)

    def test_changed_list_identity_cannot_reuse_local_key(self):
        session=EMarkedSession();key=session.get_post('B.A.95',index=30,query=True)['aid']
        session.list_title='different title'
        with self.assertRaises(c.CCNSError):session.read_colored_post('B.A.95',key)
        self.assertEqual(session.opens,0)

    def test_notice_requires_e_and_exact_lock_notice(self):
        for marker,notice in (('m04',EMarkedSession.query_notice_text),('e04','您的權限不足'),('e04','文章不存在')):
            session=EMarkedSession();session.marker=marker;session.query_notice_text=notice
            with self.assertRaises(c.CCNSQueryUnavailable):session.get_post('B.A.95',index=30,query=True)
            self.assertEqual(session.opens,0)

    def test_changed_body_on_second_read_is_not_saved(self):
        class Changed(EMarkedSession):
            def _command(self,keys,predicate,**kwargs):
                result=super()._command(keys,predicate,**kwargs)
                if self.opens==2 and keys=='\x1b[C':
                    self.terminal.feed(b'\x1b[4;1Hdifferent')
                    return self.terminal.text
                return result
        session=Changed();key=session.get_post('B.A.95',index=30,query=True)['aid']
        with patch('ccns_session.save_reading_capture',return_value=None):
            with self.assertRaisesRegex(c.CCNSError,'兩次讀取'):session.read_colored_post('B.A.95',key)


class CaptureConsistencyTests(unittest.TestCase):
    def test_local_identity_ignores_push_count_but_not_article_fields(self):
        session=EMarkedSession();key=session.get_post('B.A.95',index=30,query=True)['aid']
        session.marker='e05'
        self.assertEqual(session.get_post('B.A.95',index=30,query=True)['aid'],key)
        self.assertEqual(session.read_colored_post('B.A.95',key)['local_identity'],key)
        session.list_title='replacement'
        with self.assertRaises(c.CCNSError):session.read_colored_post('B.A.95',key)

    def test_only_invisible_space_foreground_is_equivalent(self):
        self.assertEqual(c.visible_runs([['a ',['f3','hl']],['b',['f2']]]),c.visible_runs([['a',['f3','hl']],[' ',['f7']],['b',['f2']]]))
        self.assertNotEqual(c.visible_runs([['a',['f3']]]),c.visible_runs([['a',['f7']]]))
        self.assertNotEqual(c.visible_runs([[' ',['f3','b4']]]),c.visible_runs([[' ',['f7']]]))

    def test_blank_background_continuation_is_folded_and_retained(self):
        lines=article()[:3]+['x'*77+'\\',' '*10]
        terminal=c.Terminal();terminal.feed(frame(lines,footer(1,1,1,4,100)))
        terminal.feed(b'\x1b[4;78H\x1b[1;37m\\')
        terminal.feed(b'\x1b[5;1H\x1b[35;45m'+b' '*10)
        pages=c.NumberedPages();pages.add(terminal)
        post=pages.post('Sample01',30,'B.A.95')
        self.assertTrue(post['full_content'].endswith('x'*77+' '*10))
        self.assertIn('b5',post['ansi_lines']['3'][-1][1])


class Release2288Tests(unittest.TestCase):
    def test_thread_marker_does_not_change_local_identity(self):
        before='e04 01/14 Sample ◇ title'
        after='e05 01/14 Sample ◆ title'
        self.assertEqual(c.local_article_identity('B.A.95',30,before),c.local_article_identity('B.A.95',30,after))
        self.assertNotEqual(c.local_article_identity('B.A.95',30,before),c.local_article_identity('B.A.95',31,after))

    def test_old_header_without_board_requires_verified_identity(self):
        term=c.Terminal();lines=article()[:3];lines[0]=' 作者 SampleUser (Nickname)'
        term.feed(frame(lines,footer(1,1,1,3,100)));pages=c.NumberedPages();pages.add(term)
        with self.assertRaises(c.CCNSError):pages.post('Sample01',30,'B.A.95')
        post=pages.post('Sample01',30,'B.A.95',identity_verified=True)
        self.assertEqual(post['author'],'SampleUser (Nickname)')
        self.assertIsNone(post['original_header_board'])
        self.assertEqual(post['full_content'],'\n'.join(lines))

    def test_archive_lock_blocks_competing_export_and_releases_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            with archive.archive_lock(folder):
                with self.assertRaises(archive.ArchiveBusyError):
                    with archive.archive_lock(folder):pass
            with self.assertRaises(ValueError):
                with archive.archive_lock(folder):raise ValueError('test interruption')
            with archive.archive_lock(folder):pass

    def test_relaunch_reuses_existing_app_without_starting_or_stopping_server(self):
        with patch.object(app,'existing_downloader_url',return_value='http://127.0.0.1:18761'),patch.object(app.webbrowser,'open') as opened,patch.object(app,'create_server') as server:
            app.main()
            opened.assert_called_once_with('http://127.0.0.1:18761')
            server.assert_not_called()

    def test_legacy_local_key_does_not_duplicate_same_body_after_success(self):
        with tempfile.TemporaryDirectory() as tmp,closing(app.database(Path(tmp))) as db,patch.object(app,'STATE',dict(saved=0,skipped=0)):
            session=EMarkedSession();key=session.get_post('B.A.95',index=30,query=True)['aid']
            old=session.read_colored_post('B.A.95',key);old['aid']='CCNS-E-30-0123456789abcdef'
            app.save_article(db,'B.A.95',old,site='CCNS',source_index=30)
            app.download_article(session,db,'CCNS','B.A.95',key,source_index=30,checkpoint=True)
            self.assertEqual(db.execute('select count(*) from articles').fetchone()[0],1)
            self.assertEqual(db.execute('select aid from articles').fetchone()[0],key)
            self.assertEqual(db.execute('select count(*) from article_revisions').fetchone()[0],1)
