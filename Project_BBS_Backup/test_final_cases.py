import unittest
from html.parser import HTMLParser
from PyPtt import screens, connect_core
import app
import archive
from ansi_capture import NumberedArticle
from board_session import ArticleTargetUnit
from terminal_links import TerminalLinks
from link_positions import frame_links, resolve_links
from test_scroll_capture import line

LABEL = 'https://example.com/identical-prefix'
URLS = [LABEL + '/first/', LABEL + '/second/']
HINT = '▲此頁內容會依閱讀者不同,原文未必有您的資料 (h)說明'

class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.active=None
    def handle_starttag(self,tag,attrs):
        if tag=='a': self.active=[dict(attrs)['href'],'']
    def handle_data(self,data):
        if self.active is not None: self.active[1]+=data
    def handle_endtag(self,tag):
        if tag=='a' and self.active is not None:
            self.links.append(self.active); self.active=None

class FinalCaseTests(unittest.TestCase):
    def test_screenshot_footers_recognize_article_before_personalized_capture(self):
        for page,percent in [('37/39',97),('74/80',80)]:
            screen=f'ordinary body\n瀏覽 第 {page} 頁 ({percent}%) '+HINT
            self.assertTrue(ArticleTargetUnit(screens.Target.InPost).is_match(screen,'>'))
            self.assertFalse(ArticleTargetUnit(screens.Target.PostEnd).is_match(screen,'>'))
        self.assertTrue(ArticleTargetUnit(screens.Target.PostEnd).is_match('body\n瀏覽 第 80/80 頁 (100%) '+HINT,'>'))

    def test_hint_in_body_or_missing_footer_evidence_does_not_match(self):
        for screen in [HINT+'\nunknown', '瀏覽 第 1/2 頁 (80%)',
                       '瀏覽 第 1/2 頁 (101%) '+HINT, '瀏覽 (80%) '+HINT]:
            self.assertFalse(ArticleTargetUnit(screens.Target.InPost).is_match(screen,'>'))

    def test_wrapped_target_preserves_sdk_options_and_match_limit(self):
        old=connect_core.TargetUnit(screens.Target.InPost,response='x',break_detect=True,secret=True,max_match=1)
        target=ArticleTargetUnit(old)
        screen='body\n瀏覽 第 1/2 頁 (80%) '+HINT
        self.assertTrue(target.is_match(screen,'>')); self.assertFalse(target.is_match(screen,'>'))
        self.assertEqual(target.get_response(screen),'x'); self.assertTrue(target.is_secret())
        self.assertTrue(target.is_break()); self.assertEqual(old._current_match,0)

    def test_recognized_hint_still_requires_continuous_body_and_real_end(self):
        rows=[line('row '+str(i)) for i in range(6)]
        n=NumberedArticle(allow_wrapping=True,short_wrap_retry=True)
        n.observe(rows[:4],'瀏覽 第 1/3 頁 (10%) 第 1~4 行',at_end=False,styled=rows[:4])
        footer='瀏覽 第 2/3 頁 (80%) '+HINT
        self.assertTrue(ArticleTargetUnit(screens.Target.InPost).is_match('body\n'+footer,'>'))
        n.observe(rows[1:5],footer,at_end=False,styled=rows[1:5])
        self.assertIsNone(n.assemble())
        n.observe(rows[2:],'瀏覽 第 3/3 頁 (100%) '+HINT,at_end=True,styled=rows[2:])
        self.assertEqual(n.assemble(),'\n'.join(rows)); self.assertTrue(n.personalized)

    def frame(self,chunk_size):
        wire=b'\x1b[H\x1b[2Jfirst context\n'
        for i,url in enumerate(URLS):
            wire+=b'\x1b]8;;'+url.encode()+b'\x1b\\'+LABEL.encode()+b'\x1b]8;;\x1b\\\n'
            wire+=('context '+str(i)+'\n').encode()
        decoder=TerminalLinks()
        data=b''.join(decoder.feed(wire[i:i+chunk_size]) for i in range(0,len(wire),chunk_size))
        parser=screens.VT100Parser(data,'utf-8',8)
        rows=parser.screen.split('\n')
        marks=frame_links(data,'utf-8',8,decoder.occurrences,rows)
        return decoder,rows,marks,data

    def test_identical_labels_keep_distinct_targets_across_chunk_boundaries(self):
        for size in (1,2,5,13,10000):
            with self.subTest(size=size):
                decoder,rows,marks,data=self.frame(size)
                self.assertEqual(decoder.links[LABEL],set(URLS))
                self.assertEqual([(m['row'],m['start'],m['target']) for m in marks],[(1,0,URLS[0]),(3,0,URLS[1])])
                self.assertNotIn(b'\x1b]8;',data)
                resolved=resolve_links('\n'.join(rows),[{'rows':rows,'links':marks}])
                self.assertEqual([m['target'] for m in resolved],URLS)

    def test_parser_reset_clears_positions_but_keeps_observed_global_targets(self):
        decoder,*_=self.frame(5)
        decoder.reset_frame()
        self.assertEqual(decoder.occurrences,[]); self.assertEqual(decoder.visible_bytes,0)
        self.assertEqual(decoder.targets,set(URLS))

    def test_duplicate_context_or_conflicting_target_is_not_guessed(self):
        _,rows,marks,_=self.frame(5)
        frame={'rows':rows,'links':marks}
        self.assertEqual(resolve_links('\n'.join(rows+rows),[frame]),[])
        other={'rows':rows,'links':[dict(m,target=URLS[1-i]) for i,m in enumerate(marks)]}
        self.assertEqual(resolve_links('\n'.join(rows),[frame,other]),[])

    def test_two_identical_labels_render_full_distinct_clickable_urls(self):
        _,rows,marks,_=self.frame(5)
        body='\n'.join(rows)
        links=resolve_links(body,[{'rows':rows,'links':marks}])
        for colors in ({},None):
            page=archive.article_body(body,colors,link_occurrences=links)
            parsed=Links();parsed.feed(page)
            self.assertEqual(parsed.links,[[url,url] for url in URLS])
        self.assertEqual(rows[1],rows[3])

    def test_same_line_occurrences_use_character_positions(self):
        body=LABEL+' '+LABEL
        links=[{'line':0,'start':start,'label':LABEL,'target':URLS[i]}
               for i,start in enumerate((0,len(LABEL)+1))]
        parsed=Links();parsed.feed(archive.article_body(body,{},link_occurrences=links))
        self.assertEqual(parsed.links,[[url,url] for url in URLS])

    def test_bad_position_does_not_rewrite_link(self):
        bad={'line':0,'start':1,'label':LABEL,'target':URLS[0]}
        parsed=Links();parsed.feed(archive.article_body(LABEL,{},link_occurrences=[bad]))
        self.assertEqual(parsed.links,[[LABEL,LABEL]])

    def test_complete_positional_targets_allow_manual_replacement_without_site_request(self):
        _,rows,marks,_=self.frame(5)
        body='\n'.join(rows)
        links=resolve_links(body,[{'rows':rows,'links':marks}])
        post={'full_content':body,'capture_version':app.CAPTURE_VERSION,
              'ansi_coverage':{'matched':len(rows),'total':len(rows)},
              'ansi_diagnostics':{'reached_end':True,'hyperlink_targets':URLS},
              'link_occurrences':links}
        self.assertTrue(app.manual_capture_ready(post))
        post['link_occurrences']=links[:1]
        self.assertEqual([issue['code'] for issue in app.manual_capture_issues(post)],['unmapped_url_targets'])

class IntegrationTests(unittest.TestCase):
    def test_capture_stream_positions_reach_saved_post_and_html(self):
        from types import SimpleNamespace
        from ansi_capture import read_colored_post
        decoder=FinalCaseTests()
        _,rows,_,_=decoder.frame(5)
        wire=b'\x1b[H\x1b[2Jfirst context\n'
        for i,url in enumerate(URLS):
            wire+=b'\x1b]8;;'+url.encode()+b'\x1b\\'+LABEL.encode()+b'\x1b]8;;\x1b\\\n'
            wire+=('context '+str(i)+'\n').encode()
        wire+=b'\x1b[8;1H'+('瀏覽 第 1/1 頁 (100%) 目前顯示: 第 1~5 行').encode()
        class Core:
            config=SimpleNamespace(screen_height=8)
            current_encoding='utf-8'
            def _stream_screen(self,encoding,data):
                parser=self._stream_parsers.setdefault(encoding,screens.IncrementalScreen(encoding,8))
                parser.feed(data); return parser.screen
            def send(self,message,targets):
                self._stream_parsers={}
                for start in range(0,len(wire),7):
                    self.screen=self._stream_screen('utf-8',wire[start:start+7])
                return next(i for i,t in enumerate(targets) if t.is_match(self.screen,'>'))
            def get_screen_queue(self): return [self.screen]
        core=Core()
        def get_post(board,aid):
            core.send('',[connect_core.TargetUnit(screens.Target.PostEnd)])
            return {'full_content':'\n'.join(rows[:5])}
        post=read_colored_post(SimpleNamespace(connect_core=core,get_post=get_post),'SampleBoard','Sample01')
        self.assertEqual([link['target'] for link in post['link_occurrences']],URLS)
        parsed=Links(); parsed.feed(archive.article_body(post['full_content'],post.get('ansi_lines',{}),link_occurrences=post['link_occurrences']))
        self.assertEqual(parsed.links,[[url,url] for url in URLS])

    def test_proven_continuation_is_not_duplicated_with_or_without_marker(self):
        target=URLS[0]; tail=target[len(LABEL):]
        for marker in ('','\\'):
            body=LABEL+marker+'\n'+tail+'\nordinary text'
            links=[{'line':0,'start':0,'label':LABEL,'target':target}]
            page=archive.article_body(body,{},link_occurrences=links)
            parsed=Links();parsed.feed(page)
            self.assertEqual(parsed.links,[[target,target]])
            self.assertNotIn('</a>\n'+tail,page)
            self.assertIn('ordinary text',page)

if __name__=='__main__': unittest.main()
