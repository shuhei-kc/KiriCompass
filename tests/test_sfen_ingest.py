"""End-to-end checks for .sfen results, validation and incremental batches."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kifudb.board import HIRATE_BOARD_SFEN
from kifudb.csa import parse_csa
from kifudb.db import open_for_write, open_read_only
from kifudb.export import game_to_csa
from kifudb.query import PrecedentReader
from kifudb.sfen_ingest import delete_batch, ingest_file, list_batches, scan_file


class SfenIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "test.db"
        self.path = self.root / "batch.sfen"

    def write(self, *lines: str) -> None:
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def rows(self, sql: str):
        conn = open_read_only(self.db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_max_moves_is_previewed_ingested_counted_and_exported_as_draw(self):
        self.write("startpos moves 7g7f max_moves",
                   "startpos moves 2g2f rep_draw",
                   "startpos moves 9g9f resign")
        scan = scan_file(self.path)
        self.assertEqual((scan.games, scan.skipped), (3, []))
        result = ingest_file(self.db, self.path)
        self.assertEqual((result.added, result.skipped), (3, []))
        self.assertEqual(self.rows(
            "SELECT result, end_reason FROM games ORDER BY game_id"),
            [(0, "max_moves"), (0, "sennichite"), (1, "toryo")])
        with PrecedentReader(self.db) as reader:
            candidates, _, total = reader.lookup("startpos")
            self.assertEqual(total, 3)
            max_moves = next(c for c in candidates if c.usi == "7g7f")
            self.assertEqual((max_moves.game_count, max_moves.draws,
                              max_moves.black_wins, max_moves.white_wins),
                             (1, 1, 0, 0))
            csa = game_to_csa(reader.get_game(1))
            self.assertIn("%MAX_MOVES", csa)
            restored = parse_csa(csa)
            self.assertEqual((restored.result, restored.end_reason),
                             (0, "max_moves"))
        self.assertTrue(ingest_file(self.db, self.path).unchanged)

    def test_resignation_uses_initial_turn_and_move_count(self):
        self.write(
            "startpos moves 7g7f resign",
            "startpos moves 7g7f 3c3d resign",
            f"sfen {HIRATE_BOARD_SFEN} w - 1 moves 3c3d resign",
            f"sfen {HIRATE_BOARD_SFEN} w - 1 moves 3c3d 7g7f resign")
        self.assertEqual(ingest_file(self.db, self.path).added, 4)
        self.assertEqual(self.rows("SELECT result FROM games ORDER BY game_id"),
                         [(1,), (2,), (2,), (1,)])

    def test_invalid_rows_are_skipped_without_polluting_positions(self):
        self.write("startpos moves P*5e max_moves",       # no pawn in hand
                   "startpos moves 3c3d max_moves",       # opponent's piece
                   "startpos moves 5i4i max_moves",       # own gold
                   "startpos moves 5i5h+ max_moves",      # promote king
                   "sfen bad w - 1 moves 3c3d max_moves", # malformed board
                   "startpos moves 7g7f unknown",
                   "startpos moves 7g7f max_moves")
        result = ingest_file(self.db, self.path)
        self.assertEqual((result.added, len(result.skipped)), (1, 6))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM games"), [(1,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM position_games"), [(2,)])

    def test_invalid_shared_prefix_is_reported_per_line(self):
        self.write("startpos moves P*5e resign",
                   "startpos moves P*5e max_moves")
        result = ingest_file(self.db, self.path)
        self.assertEqual((result.added, len(result.skipped)), (0, 2))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM games"), [(0,)])

    def test_valid_captures_promotion_and_drop_survive_validation(self):
        self.write("startpos moves 7g7f 3c3d 8h2b+ 3a2b B*5e max_moves")
        result = ingest_file(self.db, self.path)
        self.assertEqual((result.added, result.skipped), (1, []))
        with PrecedentReader(self.db) as reader:
            detail = reader.get_game(1)
            restored = parse_csa(game_to_csa(detail))
            self.assertEqual(len(restored.moves), 5)
            self.assertEqual(restored.result, 0)

    def seed_legacy_batch(self):
        self.write(f"sfen {HIRATE_BOARD_SFEN} w - 1 moves 3c3d resign",
                   "startpos moves 7g7f max_moves")
        # Emulate an unversioned ledger whose max_moves line was skipped.
        with patch("kifudb.sfen_ingest._TERMINALS", {"resign", "rep_draw"}):
            self.assertEqual(ingest_file(
                self.db, self.path, label="original label",
                date_str="2026-07-01 12:00:00").added, 1)
        conn = open_for_write(self.db)
        try:
            detail = json.loads(conn.execute(
                "SELECT detail FROM source_files").fetchone()[0])
            detail.pop("import_version")
            conn.execute("UPDATE source_files SET detail=?", (json.dumps(detail),))
            conn.execute("UPDATE games SET result=1")  # old white-start bug
            conn.commit()
        finally:
            conn.close()

    def test_legacy_unchanged_batch_recovers_skipped_rows_and_results_once(self):
        self.seed_legacy_batch()
        result = ingest_file(self.db, self.path, label="replacement label")
        self.assertTrue(result.rebuilt)
        self.assertEqual((result.added, result.skipped), (2, []))
        self.assertEqual(self.rows(
            "SELECT result, end_reason, black_name, started_at "
            "FROM games ORDER BY event"),
            [(2, "toryo", "original label_1", "2026-07-01 12:00:00"),
             (0, "max_moves", "original label_2", "2026-07-01 12:00:00")])
        self.assertTrue(ingest_file(self.db, self.path).unchanged)

    def test_legacy_rebuild_rolls_back_on_failure(self):
        self.seed_legacy_batch()
        before = self.rows("SELECT * FROM games")
        positions = self.rows("SELECT * FROM position_games")
        ledger = self.rows("SELECT * FROM source_files")
        with patch("kifudb.sfen_ingest._insert_game", side_effect=RuntimeError("test")):
            with self.assertRaises(RuntimeError):
                ingest_file(self.db, self.path)
        self.assertEqual(self.rows("SELECT * FROM games"), before)
        self.assertEqual(self.rows("SELECT * FROM position_games"), positions)
        self.assertEqual(self.rows("SELECT * FROM source_files"), ledger)

    def test_legacy_rebuild_still_rejects_modified_prefix(self):
        self.seed_legacy_batch()
        before = self.rows("SELECT * FROM games")
        self.write("startpos moves 2g2f max_moves")
        result = ingest_file(self.db, self.path)
        self.assertTrue(result.conflict)
        self.assertFalse(result.rebuilt)
        self.assertEqual(self.rows("SELECT * FROM games"), before)

    def test_appending_max_moves_updates_common_prefix_without_duplicates(self):
        first = "startpos moves 7g7f 3c3d resign"
        self.write(first)
        self.assertEqual(ingest_file(self.db, self.path).added, 1)
        second = "startpos moves 7g7f 8c8d max_moves"
        self.write(first, second)
        result = ingest_file(self.db, self.path)
        self.assertTrue(result.rebuilt)
        self.assertEqual(result.prefix_len, 1)
        self.assertEqual(list_batches(self.db)[0]["games"], 2)
        self.write(first, second, "startpos moves 7g7f 4c4d max_moves")
        result = ingest_file(self.db, self.path)
        self.assertFalse(result.rebuilt)
        self.assertEqual(result.added, 1)
        self.assertTrue(ingest_file(self.db, self.path).unchanged)
        self.assertEqual(list_batches(self.db)[0]["games"], 3)
        self.assertEqual(delete_batch(self.db, self.path), 4)  # includes task
        self.assertEqual(self.rows("SELECT COUNT(*) FROM position_games"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM position_stats"), [(0,)])

    def test_batch_deletion_does_not_touch_similarly_named_batch_or_public_game(self):
        self.write("startpos moves 7g7f max_moves")
        ingest_file(self.db, self.path)
        other = self.root / "batch#L00001.sfen"
        other.write_text("startpos moves 2g2f max_moves\n")
        ingest_file(self.db, other)
        conn = open_for_write(self.db)
        try:
            conn.execute("INSERT INTO games "
                         "SELECT 99, 'batch#L99999', 'other', started_at, "
                         "black_name, white_name, result, end_reason, ply_count, "
                         "initial_sfen, moves FROM games LIMIT 1")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual([b["games"] for b in list_batches(self.db)], [1, 1])
        self.assertEqual(delete_batch(self.db, self.path), 1)
        self.assertEqual(self.rows("SELECT event FROM games ORDER BY event"),
                         [("batch#L00001#L00001",), ("batch#L99999",)])

    def test_unchanged_moved_file_updates_ledger(self):
        self.write("startpos moves 7g7f max_moves")
        ingest_file(self.db, self.path)
        moved = self.root / "moved" / self.path.name
        moved.parent.mkdir()
        self.path.rename(moved)
        self.assertTrue(ingest_file(self.db, moved).unchanged)
        self.assertEqual(list_batches(self.db)[0]["path"], str(moved))

    def test_half_written_last_line_waits_instead_of_causing_conflict(self):
        first = "startpos moves 7g7f 3c3d resign"
        self.path.write_bytes((first + "\nstartpos moves 7g7f 8c").encode())
        scan = scan_file(self.path)
        self.assertEqual((scan.games, scan.skipped, scan.pending_tail),
                         (1, [], True))
        result = ingest_file(self.db, self.path)
        self.assertEqual((result.added, result.skipped, result.pending_tail),
                         (1, [], True))
        # The generator finishes the line: a plain append, not a conflict.
        self.write(first, "startpos moves 7g7f 8c8d max_moves")
        result = ingest_file(self.db, self.path)
        self.assertFalse(result.conflict)
        self.assertEqual(list_batches(self.db)[0]["games"], 2)
        self.assertTrue(ingest_file(self.db, self.path).unchanged)

    def test_complete_last_line_without_newline_is_ingested(self):
        self.path.write_bytes(b"startpos moves 7g7f resign")
        result = ingest_file(self.db, self.path)
        self.assertEqual((result.added, result.pending_tail), (1, False))

    def test_legacy_ledger_counting_unterminated_bad_line_stays_unchanged(self):
        self.path.write_bytes(b"startpos moves 7g7f resign\ngarbage")
        with patch("kifudb.sfen_ingest._read_lines",
                   lambda path: (path.read_bytes().splitlines(), None)):
            self.assertEqual(ingest_file(self.db, self.path).added, 1)
        result = ingest_file(self.db, self.path)
        self.assertTrue(result.unchanged)
        self.assertFalse(result.conflict)

    def test_bom_and_usi_position_prefix_are_accepted(self):
        self.path.write_bytes("\ufeffstartpos moves 7g7f resign\n"
                              "position startpos moves 2g2f max_moves\n"
                              .encode("utf-8"))
        result = ingest_file(self.db, self.path)
        self.assertEqual((result.added, result.skipped), (2, []))

    def test_lines_newly_readable_are_recovered_from_older_batches(self):
        self.path.write_bytes("\ufeffstartpos moves 7g7f resign\n"
                              "position startpos moves 2g2f max_moves\n"
                              .encode("utf-8"))
        # Emulate the v2 parser, which skipped both lines.
        with patch("kifudb.sfen_ingest._IMPORT_VERSION", 2), \
                patch("kifudb.sfen_ingest._parse_line",
                      lambda raw, lineno: f"{lineno}行目: unreadable"):
            self.assertEqual(ingest_file(self.db, self.path).added, 0)
        self.assertEqual(scan_file(self.path).games, 2)
        result = ingest_file(self.db, self.path)
        self.assertTrue(result.rebuilt)
        self.assertEqual((result.added, result.skipped), (2, []))
        self.assertEqual(list_batches(self.db)[0]["games"], 2)
        self.assertTrue(ingest_file(self.db, self.path).unchanged)

    def test_progress_is_reported_and_warnings_are_capped(self):
        self.write(*(["not a game"] * 30 + ["startpos moves 7g7f resign"]))
        seen, logged = [], []
        result = ingest_file(self.db, self.path, log_line=logged.append,
                             progress=lambda done, total: seen.append((done, total)))
        self.assertEqual((result.added, len(result.skipped)), (1, 30))
        self.assertEqual(seen[-1], (1, 1))
        warnings = [m for m in logged if "警告:" in m]
        self.assertEqual(len(warnings), 11)  # 10 lines + "ほか 20行"
        self.assertIn("ほか 20行", warnings[-1])

    def test_unchanged_file_is_reported(self):
        self.write("startpos moves 7g7f resign")
        ingest_file(self.db, self.path)
        logged = []
        self.assertTrue(ingest_file(self.db, self.path,
                                    log_line=logged.append).unchanged)
        self.assertEqual(logged, ["[batch] 変更なし"])

    def test_batch_lookup_uses_event_index(self):
        from kifudb.sfen_ingest import _batch_game_condition, _batch_lines_condition
        self.write("startpos moves 7g7f resign")
        ingest_file(self.db, self.path)
        for build in (_batch_game_condition, _batch_lines_condition):
            condition, params = build("batch")
            plan = " ".join(str(row) for row in self.rows_with(
                f"EXPLAIN QUERY PLAN SELECT game_id FROM games WHERE {condition}",
                params))
            self.assertIn("USING INDEX", plan)
            self.assertNotIn("SCAN games", plan)

    def rows_with(self, sql: str, params):
        conn = open_read_only(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
