import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

import app
import local_state
from scroll_capture import ScrollCapture, visible_row, overlap_candidates
from test_scroll_capture import line, observe


class PatchTests(unittest.TestCase):
    def test_only_terminal_tail_controls_are_normalized(self):
        for count in (1,2,4,6):
            self.assertEqual(visible_row(('same text'+'\x08'*count,())),('same text',()))
        self.assertEqual(visible_row(('a\x08b',())),('a\x08b',()))
        self.assertEqual(visible_row(('   a  ',())),('   a  ',()))
        colored=('same text\x08',(('same text\x08',('f1',)),))
        self.assertEqual(visible_row(colored),('same text',(('same text',('f1',)),)))
        mismatched=('same text\x08',(('other',('f1',)),))
        self.assertEqual(visible_row(mismatched),mismatched)

    def test_tail_control_difference_does_not_break_continuity_or_lose_color(self):
        rows=[line('a',('f1',)),line('b'),line('c'),line('d'),line('e')]
        c=ScrollCapture()
        observe(c,rows[:4],1,4)
        frame=[line('b\x08\x08'),*rows[2:]]
        observe(c,frame,2,5,True)
        self.assertEqual([t for t,_ in c.result],list(rows))
        self.assertEqual(c.result[0][1],(('a',('f1',)),))
        self.assertEqual(c.trailing_controls_removed,2)

    def test_foreground_color_changes_are_not_ignored(self):
        c=ScrollCapture()
        observe(c,[line('a'),line('b'),line('c')],1,3)
        observe(c,[line('b\x08',('f1',)),line('c'),line('d')],2,4,True)
        self.assertIsNone(c.result)
        self.assertEqual(c.error,'overlap_missing')

    def test_tail_blank_is_preserved_using_prior_offset_evidence(self):
        rows=[line(str(i)) for i in range(9)]
        c=ScrollCapture()
        observe(c,rows[:5],1,4)
        observe(c,rows[1:6],2,5)
        observe(c,rows[2:7],3,6)
        observe(c,rows[3:8],4,7)
        observe(c,[*rows[4:],line('')],5,8,True)
        self.assertEqual([t for t,_ in c.result],[*rows,''])
        self.assertEqual(c.expected_end_lines,9)
        self.assertEqual(c.end_padding_rows,1)

    def test_extra_visible_text_and_colored_tail_are_not_padding(self):
        for extra in (line('unexpected'),line(' ',('b1',))):
            with self.subTest(extra=repr(extra)):
                rows=[line(str(i)) for i in range(9)]
                c=ScrollCapture()
                observe(c,rows[:5],1,4)
                observe(c,rows[1:6],2,5)
                observe(c,rows[2:7],3,6)
                observe(c,[*rows[3:],extra],4,8,True)
                self.assertIsNone(c.result)
                self.assertEqual(c.error,'end_line_count_mismatch')

    def test_source_advance_disambiguates_repeated_text(self):
        pattern=[str(i) for i in range(10)];pattern[1]=pattern[2]=''
        previous=[(pattern[i%10],()) for i in range(23)]
        current=[(pattern[i%10],()) for i in range(1,24)]
        c=ScrollCapture();c.previous=previous;c.last_range=(20,42);c.offsets.extend([0,0])
        choices=c.seam(current)
        self.assertEqual(choices,[(12,0),(22,0)])
        self.assertEqual(c.resolve_ambiguity(choices,21),([(22,0)],True))
        c.offsets.clear()
        self.assertEqual(c.resolve_ambiguity(choices,21),(choices,False))
        c.offsets.extend([0,0])
        self.assertEqual(c.resolve_ambiguity(choices,25),(choices,False))

    def test_wrap_prefix_prevents_guessing_a_source_advance(self):
        c=ScrollCapture();c.last_range=(1,4);c.offsets.extend([0,0])
        marker=('x'*78+'\\',(('x'*78,('f1',)),('\\',('f7','hl'))))
        c.previous=[marker,('tail',()),('a',()),('b',())]
        # Removing the first physical row leaves an unfinished source line.
        choices=[(3,0),(2,0)]
        self.assertEqual(c.resolve_ambiguity(choices,2),([(2,0)],True))

    def test_first_and_latest_failure_survive_a_recovery(self):
        c=ScrollCapture();rows=[line(str(i)) for i in range(7)]
        observe(c,rows[:4],1,4)
        observe(c,[line('first failure'),line('x'),line('y')],2,5)
        first=c.first_failure
        observe(c,rows[1:5],2,5)
        self.assertEqual(c.waiting,0)
        observe(c,[line('latest failure'),line('m'),line('n')],3,6)
        self.assertEqual(c.first_failure,first)
        self.assertNotEqual(c.last_failure,first)
        self.assertEqual(c.diagnostics()['last_failed_seam']['observed'][0][0],'latest failure')

    def test_latest_failure_is_rendered_beside_first(self):
        diagnostic={'numbering':{'screen_overlap':{
            'first_failed_seam':{'accepted':[['accepted first',[]]],'observed':[['first failure',[]]]},
            'last_failed_seam':{'accepted':[['accepted latest',[]]],'observed':[['latest failure',[]]]}}}}
        sample={'screen':'article\nfooter','key':'down','height':24,'encoding':'utf-8','at_end':False,'linked_screen':False,'colors':[[]],'raw':b'article'}
        with tempfile.TemporaryDirectory() as directory,patch.object(local_state,'LOG_DIRECTORY',Path(directory)):
            path=local_state.save_reading_capture('SampleBoard','Sample01','line',[sample],diagnostic,'test')
            text=path.read_text(encoding='utf-8')
            self.assertIn('第一次接合失敗',text);self.assertIn('最近一次接合失敗',text)
            self.assertIn('first failure',text);self.assertIn('latest failure',text)

    def test_manual_rejection_reasons_are_separate_and_complete(self):
        post={'full_content':'8;; text','capture_version':app.CAPTURE_VERSION,
              'ansi_coverage':{'matched':1,'total':2},
              'ansi_diagnostics':{'reached_end':False,'hyperlink_targets':['https://example.com/full/']}}
        issues=app.manual_capture_issues(post)
        self.assertEqual({v['code'] for v in issues},{'coverage_incomplete','end_unconfirmed','control_code_residue','unmapped_url_targets'})
        self.assertEqual(issues[-1]['targets'],['https://example.com/full/'])
        self.assertFalse(app.manual_capture_ready(post))


if __name__=='__main__':unittest.main()
