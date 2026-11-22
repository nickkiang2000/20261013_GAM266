import unittest
from ansi_capture import ColorScreen, CapturedLine, NumberedArticle
from color_evidence import reconcile, FG, BG, BRIGHT, ALL


def painted(wire, height=3):
    frame=ColorScreen(height).replay(wire,'utf-8')
    return [CapturedLine(str(row),tuple(row.runs()),tuple(row.specified)) for row in frame]


class ColorEvidenceTests(unittest.TestCase):
    def test_omitted_foreground_brightness_and_background_are_resolved_independently(self):
        prior=painted(b'\x1b[1;35;43mtext')[0]
        current=painted(b'\x1b[43mtext')[0]
        result=reconcile(prior,current)
        self.assertEqual(result[0],(('text',('f5','b3','hl')),))
        self.assertEqual(result[1],(ALL,)*4)

    def test_explicit_reset_is_not_mistaken_for_missing_color(self):
        prior=painted(b'\x1b[1;35mtext')[0]
        current=painted(b'\x1b[mtext')[0]
        self.assertIsNone(reconcile(prior,current))
        self.assertEqual(current.color_mask,(ALL,)*4)

    def test_explicit_foreground_or_background_or_brightness_changes_still_fail(self):
        for old,new in [(b'\x1b[31mtext',b'\x1b[32mtext'),
                        (b'\x1b[41mtext',b'\x1b[42mtext'),
                        (b'\x1b[1mtext',b'\x1b[22mtext')]:
            self.assertIsNone(reconcile(painted(old)[0],painted(new)[0]))

    def test_different_text_or_invalid_runs_are_not_reconciled(self):
        self.assertIsNone(reconcile(painted(b'\x1b[31malpha')[0],painted(b'beta')[0]))
        bad=CapturedLine('text',(('wrong',('f1',)),),(0,)*4)
        self.assertIsNone(reconcile(bad,painted(b'text')[0]))

    def test_unknown_colors_do_not_leak_between_unrelated_source_lines(self):
        n=NumberedArticle()
        first=painted(b'\x1b[35malpha\nbeta',3)[:2]
        second=painted(b'beta\ngamma',3)[:2]
        n.observe(first,'瀏覽 第 1~2 行',at_end=False,styled=first)
        n.observe(second,'瀏覽 第 2~3 行',at_end=True,styled=second)
        self.assertIn(('beta',('f5',)),n.colors[2])
        self.assertEqual(n.colors[3],(('gamma',()),))
        self.assertEqual(n.color_resolved_rows,{2})

    def test_numbered_page_assembly_keeps_explicit_source_color_at_overlap(self):
        n=NumberedArticle(page_mode=True)
        a=painted(b'alpha\n\x1b[1;35mbeta',3)[:2]
        b=painted(b'beta\ngamma',3)[:2]
        n.observe(a,'瀏覽 第 1~2 行',at_end=False,styled=a)
        n.observe(b,'瀏覽 第 2~3 行',at_end=True,styled=b)
        self.assertEqual(n.color_conflicts,0)
        self.assertEqual(n.assemble(),'alpha\nbeta\ngamma')
        self.assertEqual(n.colors[2],(('beta',('f5','hl')),))
        self.assertEqual(n.diagnostics()['omitted_sgr_resolved_rows'],1)

    def test_known_conflict_is_not_cleared_by_later_missing_sgr(self):
        n=NumberedArticle()
        for wire in [b'\x1b[31malpha\nbeta',b'\x1b[32malpha\nbeta',b'alpha\nbeta']:
            rows=painted(wire,3)[:2]
            n.observe(rows,'瀏覽 第 1~2 行',at_end=True,styled=rows)
        self.assertGreater(n.color_conflicts,0)
        self.assertIn(1,n.color_conflict_rows)

    def test_cursor_fill_and_clipping_keep_attribute_evidence_in_step_with_text(self):
        rows=painted(b'\x1b[31mabc\x1b[1;2HZ\x1b[K')
        self.assertEqual(rows[0],'aZ')
        self.assertEqual(len(rows[0].color_mask),2)
        self.assertEqual(rows[0].color_mask,(FG,FG))

    def test_fully_specified_legacy_runs_are_not_rewritten(self):
        a=CapturedLine('text',(('te',('f1',)),('xt',('f1',))))
        b=CapturedLine('text',(('text',('f1',)),))
        self.assertIsNone(reconcile(a,b))
        self.assertEqual(reconcile(a,a)[0],a.color_runs)

    def test_inverse_and_explicit_default_channels_remain_conservative(self):
        for control in [b'\x1b[7m',b'\x1b[27m',b'\x1b[39;49;22m']:
            current=painted(control+b'text')[0]
            self.assertEqual(current.color_mask,(ALL,)*4)


if __name__=='__main__':unittest.main()
