"""Regression tests for KIF records as written by ShogiHome / Kifu for Windows."""

from __future__ import annotations

import unittest

from kifudb.csa import RESULT_BLACK, RESULT_WHITE
from kifudb.kif import parse_kif

HEADER = "手合割：平手\n先手：A\n後手：B\n手数----指手---------消費時間--\n"


class KifTests(unittest.TestCase):
    def test_branch_marker_does_not_break_the_main_line(self) -> None:
        # 行末の '+' は変化のある手の印。時間あり・なしの両方で付く。
        text = HEADER + (
            "   1 ７六歩(77)   ( 0:01/00:00:01)\n"
            "   2 ３四歩(33)   ( 0:01/00:00:01)+\n"
            "   3 ２二角成(88)+\n"
            "   4 同　銀(31)   ( 0:01/00:00:02)\n"
            "   5 投了\n"
            "まで4手で後手の勝ち\n"
            "\n"
            "変化：2手\n"
            "   2 ８四歩(83)   ( 0:01/00:00:01)\n")
        record = parse_kif(text, "branch.kif")
        self.assertEqual(len(record.moves), 4)
        self.assertEqual((record.end_reason, record.result),
                         ("toryo", RESULT_WHITE))

    def test_foul_loss_footer_names_the_loser(self) -> None:
        for loser, expected in (("先手", RESULT_WHITE), ("後手", RESULT_BLACK)):
            with self.subTest(loser=loser):
                text = HEADER + (
                    "   1 ７六歩(77)   ( 0:01/00:00:01)\n"
                    "   2 ３四歩(33)   ( 0:01/00:00:01)\n"
                    "   3 反則負け   ( 0:01/00:00:02)\n"
                    f"まで2手で{loser}の反則負け\n")
                record = parse_kif(text, "foul.kif")
                self.assertEqual((record.end_reason, record.result),
                                 ("illegal_move", expected))

    def test_foul_win_footer_is_unchanged(self) -> None:
        text = HEADER + (
            "   1 ７六歩(77)   ( 0:01/00:00:01)\n"
            "   2 ３四歩(33)   ( 0:01/00:00:01)\n"
            "   3 反則勝ち   ( 0:01/00:00:02)\n"
            "まで2手で先手の反則勝ち\n")
        record = parse_kif(text, "foul-win.kif")
        self.assertEqual((record.end_reason, record.result),
                         ("illegal_move", RESULT_BLACK))


if __name__ == "__main__":
    unittest.main()
