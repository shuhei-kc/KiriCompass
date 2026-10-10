"""Regression checks for pasted positions, pagination and engine launchers."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kifudb.board import Position, apply_move16, normalize_sfen_main, usi_to_move16
from kifudb.query import PrecedentReader
from kifudb.sfen_ingest import ingest_file


class LookupTests(unittest.TestCase):
    def test_pasted_sfen_formats_search_the_same_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "game.sfen"
            path.write_text("startpos moves 7g7f max_moves\n")
            db = root / "test.db"
            ingest_file(db, path)
            initial = Position()
            initial.set_hirate()
            sfen = initial.sfen_key_string()
            with PrecedentReader(db) as reader:
                for text in (sfen, sfen + " 1", "sfen " + sfen + " 1",
                             "position sfen " + sfen + " 1",
                             "startpos", "position startpos",
                             "  position   sfen  " + sfen + " 1  "):
                    with self.subTest(text=text):
                        parsed = Position()
                        parsed.set_sfen(normalize_sfen_main(text))
                        self.assertEqual(parsed.sfen_key_string(), sfen)
                        candidates, precedents, count = reader.lookup(text)
                        self.assertEqual(count, 1)
                        self.assertEqual(candidates[0].usi, "7g7f")
                        self.assertEqual(len(precedents), 1)

    def test_pagination_preserves_repeated_visits_across_game_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "test.db"
            moves = ["7g7f", "3c3d"] + ["5i5h", "5a5b", "5h5i", "5b5a"] * 2
            for name, day in (("first", "01"), ("second", "02")):
                path = root / f"{name}.sfen"
                path.write_text("startpos moves " + " ".join(moves) + " rep_draw\n")
                ingest_file(db, path, date_str=f"2026-07-{day} 12:00:00")
            pos = Position()
            pos.set_hirate()
            for usi in moves[:2]:
                apply_move16(pos, usi_to_move16(usi))
            with PrecedentReader(db) as reader:
                sfen = pos.sfen_key_string()
                expected = reader.precedents_page(sfen, limit=20)
                self.assertEqual(len(expected), 6)
                self.assertEqual([p.ply for p in expected], [10, 6, 2] * 2)
                for size in (1, 2, 4):
                    for sources in (None, {"other"}):
                        with self.subTest(page_size=size, sources=sources):
                            result, before = [], None
                            for _ in range(10):  # bounded if pagination regresses
                                page = reader.precedents_page(
                                    sfen, limit=size, before=before,
                                    sources=sources, start_rank=len(result))
                                if not page:
                                    break
                                result.extend(page)
                                last = page[-1]
                                before = (last.sort_key, last.game_id, last.ply)
                            self.assertEqual(result, expected)


@unittest.skipIf(sys.platform == "win32", "POSIX launcher execution")
class LauncherTests(unittest.TestCase):
    def test_relative_database_is_found_from_another_working_directory(self):
        cli = Path(__file__).resolve().parents[1] / "tools" / "usi_engine.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build = root / "build"
            run = root / "run"
            build.mkdir()
            run.mkdir()
            game = root / "game.sfen"
            game.write_text("startpos moves 7g7f max_moves\n")
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            for index, relative in enumerate(("./my db.db", "sub/my db.db")):
                with self.subTest(db=relative):
                    ingest_file(build / relative, game)
                    launcher = root / f"engine {index}.command"
                    subprocess.run(
                        [sys.executable, "-B", str(cli), "--db", relative,
                         "--sync-file", str(root / "sync.json"),
                         "--make-launcher", str(launcher)],
                        cwd=build, env=env, check=True, capture_output=True,
                        text=True, timeout=10)
                    result = subprocess.run(
                        ["/bin/sh", str(launcher)], cwd=run, env=env,
                        input="isready\nposition startpos\ngo\nquit\n",
                        check=True, capture_output=True, text=True, timeout=10)
                    self.assertNotIn("DB not found", result.stdout)
                    self.assertIn("readyok", result.stdout)
                    self.assertIn("bestmove 7g7f", result.stdout)
                    self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
