"""Regression tests for input validation and .sfen batch isolation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kifudb.board import Position
from kifudb.csa import CsaParseError, parse_csa
from kifudb.db import open_for_write
from kifudb.ingest import ingest_folder
from kifudb.sfen_ingest import delete_batch


class RegressionTests(unittest.TestCase):
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
