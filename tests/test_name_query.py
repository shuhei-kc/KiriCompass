"""対局者名の検索式: 先手 (b:) / 後手 (w:) の指定。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kifudb.board import Position
from kifudb.ingest import ingest_folder
from kifudb.query import PrecedentReader, _name_query_condition

GAMES = {
    # ファイル名: (先手, 後手)
    "g1": ("Frieren_v1", "Sora_Ginko_x"),
    "g2": ("Sora_Ginko", "Frieren"),
    "g3": ("other", "Frieren"),
    "g4": ("SoraXGinko", "abc"),
}


class NameQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        folder = root / "kifu"
        folder.mkdir()
        for i, (stem, (black, white)) in enumerate(GAMES.items()):
            text = (f"V2.2\nN+{black}\nN-{white}\n$START_TIME:2026/01/0{i + 1} 10:00:00\n"
                    "PI\n+\n+7776FU\n-3334FU\n%TORYO\n")
            (folder / f"{stem}.csa").write_text(text, encoding="utf-8")
        cls.db = root / "test.db"
        stats = ingest_folder(cls.db, folder)
        assert (stats.added, stats.errors) == (len(GAMES), 0), stats
        start = Position()
        start.set_hirate()
        cls.sfen = start.sfen_key_string()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def names(self, query: str) -> set[tuple[str, str]]:
        with PrecedentReader(self.db) as reader:
            return {(p.black_name, p.white_name) for p in reader.precedents_page(self.sfen, name_query=query)}

    def test_either_side_as_before(self) -> None:
        self.assertEqual(self.names("Frieren"), {GAMES["g1"], GAMES["g2"], GAMES["g3"]})

    def test_black_and_white_only(self) -> None:
        self.assertEqual(self.names("b:Frieren"), {GAMES["g1"]})
        self.assertEqual(self.names("w:Frieren"), {GAMES["g2"], GAMES["g3"]})
        self.assertEqual(self.names("b:Frieren w:Sora_Ginko"), {GAMES["g1"]})
        self.assertEqual(self.names("b:Frieren OR w:Frieren"), {GAMES["g1"], GAMES["g2"], GAMES["g3"]})

    def test_exclude_and_case(self) -> None:
        self.assertEqual(self.names("Frieren -b:Frieren"), {GAMES["g2"], GAMES["g3"]})
        # 大文字の B: / W: も同じ。ASCII の名前は大文字小文字を区別しない
        self.assertEqual(self.names("B:frieren"), {GAMES["g1"]})

    def test_underscore_is_literal(self) -> None:
        # _ は LIKE のワイルドカードにしない (SoraXGinko は当たらない)
        self.assertEqual(self.names("Sora_Ginko"), {GAMES["g1"], GAMES["g2"]})
        self.assertEqual(self.names("w:Sora_Ginko"), {GAMES["g1"]})

    def test_prefix_without_name_is_literal(self) -> None:
        sql, params = _name_query_condition("b:")
        self.assertEqual(params, ["%b:%", "%b:%"])
        self.assertIn("white_name", sql)


if __name__ == "__main__":
    unittest.main()
