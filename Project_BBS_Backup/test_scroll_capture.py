import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ansi_capture import CapturedLine, NumberedArticle
from scroll_capture import ScrollCapture, overlap_candidates, wrap_prefix


def line(text, style=()):
    return CapturedLine(text, ((text, style),) if style else ())


def observe(capture, rows, first, last, end=False):
    capture.observe(rows, first, last, end)


class ScrollTests(unittest.TestCase):
    def test_variable_scroll_retains_blanks_and_colors(self):
        rows = [line('alpha', ('f1',)), line('beta'), line(''), line('gamma'), line('delta'), line('omega')]
        capture = ScrollCapture()
        observe(capture, rows[:4], 1, 4)
        observe(capture, rows[1:5], 2, 5)
        observe(capture, rows[3:], 3, 6, True)
        self.assertEqual([text for text, _ in capture.result], list(rows))
        self.assertEqual(capture.result[0][1], (('alpha', ('f1',)),))
        self.assertEqual([m['overlaps'] for m in capture.moves], [[3], [2]])

    def test_same_reported_end_can_still_supply_new_content(self):
        rows = [line(str(i)) for i in range(6)]
        capture = ScrollCapture()
        observe(capture, rows[:4], 1, 4)
        observe(capture, rows[1:5], 2, 4)
        self.assertEqual(len(capture.rows), 5)
        observe(capture, rows[2:], 3, 6, True)
        self.assertIsNotNone(capture.result)

    def test_unmatched_frame_waits_and_later_overlap_recovers(self):
        capture = ScrollCapture()
        rows = [line(str(i)) for i in range(5)]
        observe(capture, rows[:4], 1, 4)
        observe(capture, [line('bad'), *rows[2:]], 2, 5)
        self.assertEqual(capture.waiting, 1)
        self.assertIsNone(capture.result)
        observe(capture, rows[1:], 2, 5, True)
        self.assertEqual(capture.waiting, 0)
        self.assertEqual([r[0] for r in capture.result], list(rows))

    def test_gap_ambiguous_repeats_and_color_changes_cannot_save(self):
        for previous, current in [
            ([line('a'), line('b'), line('c')], [line('x'), line('y'), line('z')]),
            ([line('a'), line('b'), line('a'), line('b')], [line('a'), line('b'), line('a'), line('b')]),
            ([line('a'), line('b'), line('c')], [line('b', ('f1',)), line('c'), line('d')]),
        ]:
            capture = ScrollCapture()
            observe(capture, previous, 1, len(previous))
            observe(capture, current, 2, len(previous)+1, True)
            self.assertIsNone(capture.result)
            self.assertEqual(capture.waiting, 1)

    def test_bright_marker_at_78_79_80_with_combined_color_run(self):
        for width in (78, 79, 80):
            prefix = 'x' * (width-1)
            wrapped = line(prefix + chr(92), ('f7', 'hl'))
            self.assertEqual(wrap_prefix((str(wrapped), wrapped.color_runs))[0], prefix)
            literal = line(prefix + chr(92), ('f7',))
            self.assertIsNone(wrap_prefix((str(literal), literal.color_runs)))
            capture = ScrollCapture()
            observe(capture, [wrapped, line('tail'), literal, line('end')], 1, 3, True)
            self.assertEqual([r[0] for r in capture.result], [prefix+'tail', str(literal), 'end'])

    def test_first_screen_pending_wrap_can_continue_across_windows(self):
        prefix = 'x'*78
        wrapped = CapturedLine(prefix+chr(92), ((prefix, ('f1',)), (chr(92), ('f7','hl'))))
        rows = [line('a'), line('b'), wrapped, line('tail'), line('end')]
        capture = ScrollCapture()
        observe(capture, rows[:3], 1, 3)
        self.assertIsNone(capture.result)
        observe(capture, rows[1:], 2, 4, True)
        self.assertEqual([r[0] for r in capture.result], ['a','b',prefix+'tail','end'])

    def test_end_count_and_start_are_required_and_padding_is_bounded(self):
        capture = ScrollCapture()
        observe(capture, [line('a'), line('b')], 2, 3, True)
        self.assertIsNone(capture.result)
        capture = ScrollCapture()
        observe(capture, [line('a'), line('b'), line(''), line('')], 1, 3, True)
        self.assertEqual([r[0] for r in capture.result], ['a','b','',''])
        capture = ScrollCapture()
        observe(capture, [line('a'), line('b'), line('unexpected'), line('extra')], 1, 2, True)
        self.assertIsNone(capture.result)

    def test_complete_target_is_retained_even_when_display_label_is_cut_off(self):
        from terminal_links import TerminalLinks
        decoder = TerminalLinks()
        target = 'https://example.com/' + 'long-path-' * 20
        stream = b'\x1b]8;;' + target.encode() + b'\x07cut-off-label\x1b]8;;\x07'
        self.assertEqual(decoder.feed(stream), b'cut-off-label')
        self.assertEqual(decoder.targets, {target})
        self.assertEqual(decoder.links, {})

    def test_extra_first_line_is_verified_by_following_overlap(self):
        capture = ScrollCapture()
        rows = [line(str(i)) for i in range(6)]
        observe(capture, rows[:4], 1, 3)
        observe(capture, rows[1:5], 2, 4)
        observe(capture, rows[2:], 3, 5, True)
        self.assertEqual([r[0] for r in capture.result], list(rows))

    def test_numbered_reader_uses_down_and_waits_through_bad_frame(self):
        from board_session import BoardSession
        from PyPtt import command
        rows = [line(str(i)) for i in range(6)]
        frames = iter([(rows[:4],1,4,False), ([line('bad'),*rows[2:5]],2,5,False),
                       (rows[1:5],2,5,False), (rows[2:],3,6,True)])
        numbering = NumberedArticle(allow_wrapping=True, short_wrap_retry=True)
        session = BoardSession(SimpleNamespace(connect_core=SimpleNamespace(_decode_screen=Mock(),send=Mock()),cursor='>'))
        def send(key):
            frame,first,last,end=next(frames)
            numbering.observe(frame,f'瀏覽 第 1/2 頁 ({100 if end else 20}%) 第 {first}~{last} 行',at_end=end,styled=frame)
            return 0 if end else 1
        with patch.object(session,'_send_article_key',side_effect=send) as keys:
            self.assertEqual(session._read_numbered_article(lambda:numbering),'\n'.join(rows))
        self.assertEqual([c.args[0] for c in keys.call_args_list],[command.enter*2,*[command.down]*3])

    def test_real_failed_screen_pairs_have_unique_color_matched_overlap(self):
        fixtures=json.loads((Path(__file__).parent/'tests/fixtures/scroll_v223.json').read_text(encoding='utf-8'))
        # 3660 contains an interactive source-view redraw, tracked separately.
        # The other 28 multi-frame records all have uniquely verifiable seams.
        checked=[]
        for record in fixtures:
            if record.get('special_redraw') or len(record['frames'])<2: continue
            windows=[[(text,tuple((v,tuple(s)) for v,s in runs))
                      for text,runs in zip(f['screen'].split('\n')[:-1],f['colors'])] for f in record['frames']]
            for previous,current in zip(windows,windows[1:]):
                with self.subTest(index=record['index']):
                    self.assertEqual(len(overlap_candidates(previous,current)),1)
            checked.append(record['index'])
        self.assertEqual(len(checked),28)

    def test_reader_bounds_unmatched_frames_and_does_not_return_partial_body(self):
        from board_session import BoardSession
        numbering=NumberedArticle(allow_wrapping=True,short_wrap_retry=True)
        session=BoardSession(SimpleNamespace(connect_core=SimpleNamespace(_decode_screen=Mock(),send=Mock()),cursor='>'))
        count=0
        def send(key):
            nonlocal count
            count+=1
            rows=[line(str(i)) for i in range(4)] if count==1 else [line('unknown'+str(i)) for i in range(4)]
            numbering.observe(rows,'瀏覽 第 1/2 頁 (20%) 第 1~4 行',at_end=False,styled=rows)
            return 1
        with patch.object(session,'_send_article_key',side_effect=send):
            self.assertIsNone(session._read_numbered_article(lambda:numbering))
        self.assertEqual(count,7)


if __name__=='__main__':
    unittest.main()
