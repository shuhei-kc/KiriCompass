"""Regression tests for input validation and .sfen batch isolation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kifudb.board import Position
from kifudb.csa import CsaParseError, parse_csa
from kifudb.db import open_for_write
from kifudb.ingest import detect_source, ingest_folder
from kifudb.query import game_url
from kifudb.sfen_ingest import delete_batch


class RegressionTests(unittest.TestCase):
    def test_wcsc_official_archive_filename_is_recognized(self) -> None:
        self.assertEqual(
            detect_source("WCSC36-U7-nshogi-478shogi"), "wcsc")
        self.assertEqual(
            detect_source("WCSC36-U1-ponkotsu-test-478shogi"), "wcsc")
        for private_name in (
                "WCSC36-研究メモ", "WCSC36-U7-onlyone", "WCSC36-X7-a-b"):
            self.assertEqual(detect_source(private_name), "other")
        self.assertEqual(
            game_url("wcsc", "WCSC36+foo+bar"),
            "https://www.computer-shogi.org/live/wcsc36/html/"
            "WCSC36_foo_bar.html")

    def test_wcsc_official_filename_overrides_descriptive_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "kifu"
            folder.mkdir()
            stem = "WCSC36-U7-nshogi-478shogi"
            text = (
                "V2.2\nN+nshogi\nN-478shogi\n"
                "$EVENT:第36回世界コンピュータ将棋選手権二次予選7回戦\n"
                "$START_TIME:2026/05/04 16:00:00\n"
                "PI\n+\n+7776FU\n%TORYO\n")
            (folder / f"{stem}.csa").write_bytes(text.encode("cp932"))
            db_path = root / "test.db"

            stats = ingest_folder(db_path, folder)
            self.assertEqual((stats.added, stats.errors), (1, 0))
            conn = open_for_write(db_path)
            try:
                event, source = conn.execute(
                    "SELECT event, source FROM games").fetchone()
            finally:
                conn.close()
            self.assertEqual((event, source), (stem, "wcsc"))
            self.assertIsNone(game_url(source, event))

    def test_sfen_batch_delete_treats_underscore_as_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "test.db"
            conn = open_for_write(db_path)
            try:
                for event in ("foo_#L00001", "fooa#L00001"):
                    conn.execute(
                        "INSERT INTO games "
                        "(event, source, started_at, black_name, white_name, "
                        "result, end_reason, ply_count, initial_sfen, moves) "
                        "VALUES (?, 'sfen', '', '', '', NULL, 'task', 0, NULL, ?)",
                        (event, b""))
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(delete_batch(db_path, root / "foo_.sfen"), 1)
            conn = open_for_write(db_path)
            try:
                remaining = [row[0] for row in conn.execute(
                    "SELECT event FROM games ORDER BY event")]
            finally:
                conn.close()
            self.assertEqual(remaining, ["fooa#L00001"])

    def test_csa_rejects_drop_without_piece_in_hand(self) -> None:
        with self.assertRaises(CsaParseError):
            parse_csa("V2.2\nPI\n+\n+0055FU\n%TORYO\n", "bad.csa")

    def test_csa_allows_drop_when_piece_is_in_hand(self) -> None:
        record = parse_csa(
            "V2.2\nPI\nP+00FU\n+\n+0055FU\n%TORYO\n", "valid-drop.csa")
        self.assertEqual(len(record.moves), 1)

    def test_bad_csa_does_not_stop_folder_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "kifu"
            folder.mkdir()
            (folder / "good.csa").write_text(
                "V2.2\nPI\n+\n+7776FU\n%TORYO\n", encoding="utf-8")
            (folder / "bad.csa").write_text(
                "V2.2\nPI\n+\n+0055FU\n%TORYO\n", encoding="utf-8")
            db_path = root / "test.db"

            stats = ingest_folder(db_path, folder)
            self.assertEqual((stats.added, stats.errors), (1, 1))
            conn = open_for_write(db_path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM games").fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "SELECT status FROM source_files WHERE path LIKE '%bad.csa'").fetchone()[0],
                    "error")
            finally:
                conn.close()

    def test_sfen_rejects_rank_wider_than_nine_squares(self) -> None:
        with self.assertRaises(ValueError):
            Position().set_sfen("9P/9/9/9/9/9/9/9/9 b -")

    def test_standard_sfen_remains_accepted(self) -> None:
        pos = Position()
        pos.set_sfen("lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b -")
        self.assertEqual(pos.sfen_key_string(),
                         "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b -")


if __name__ == "__main__":
    unittest.main()
