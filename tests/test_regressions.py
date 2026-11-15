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
        for edition in range(33, 37):
            self.assertEqual(
                detect_source(f"WCSC{edition}-U7-nshogi-478shogi"),
                "wcsc")
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

    def test_wcsc_official_and_live_records_merge_in_either_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official_dir = root / "official"
            live_dir = root / "live"
            official_dir.mkdir()
            live_dir.mkdir()
            official_stem = "WCSC36-U7-nshogi-478shogi"
            live_stem = (
                "WCSC36+U7_1-900-5F+nshogi+478shogi+20260504160000")
            official_text = (
                "V2.2\nN+ねえ将棋\nN-四七八将棋\n"
                "$EVENT:第36回世界コンピュータ将棋選手権二次予選7回戦\n"
                "$START_TIME:2026/05/04 16:00:01\n"
                "PI\n+\n+7776FU\n%ILLEGAL_MOVE\n")
            live_text = (
                f"V2.2\nN+nshogi\nN-478shogi\n$EVENT:{live_stem}\n"
                "$START_TIME:2026/05/04 16:00:00\n"
                "PI\n+\n+7776FU\n%-ILLEGAL_ACTION\n")
            (official_dir / f"{official_stem}.csa").write_bytes(
                official_text.encode("cp932"))
            (live_dir / f"{live_stem}.csa").write_text(
                live_text, encoding="utf-8")

            snapshots = []
            for index, folders in enumerate(
                    ((official_dir, live_dir), (live_dir, official_dir))):
                db_path = root / f"order-{index}.db"
                first = ingest_folder(db_path, folders[0])
                second = ingest_folder(db_path, folders[1])
                self.assertEqual((first.added, first.errors), (1, 0))
                self.assertEqual(
                    (second.added, second.duplicates, second.errors),
                    (0, 1, 0))
                conn = open_for_write(db_path)
                try:
                    row = conn.execute(
                        "SELECT event, source, started_at, black_name, "
                        "white_name, result, end_reason FROM games"
                    ).fetchone()
                    position_count = conn.execute(
                        "SELECT COUNT(*) FROM position_games").fetchone()[0]
                    ledger_ids = conn.execute(
                        "SELECT COUNT(DISTINCT game_id), COUNT(*) "
                        "FROM source_files WHERE game_id IS NOT NULL"
                    ).fetchone()
                finally:
                    conn.close()
                self.assertEqual(position_count, 2)
                self.assertEqual(ledger_ids, (1, 2))
                self.assertIsNotNone(game_url(row[1], row[0], row[2]))
                snapshots.append(row)
                repeat_first = ingest_folder(db_path, folders[0])
                repeat_second = ingest_folder(db_path, folders[1])
                self.assertEqual(
                    (repeat_first.skipped_unchanged,
                     repeat_second.skipped_unchanged), (1, 1))

            expected = (live_stem, "wcsc", "2026-05-04 16:00:00",
                        "ねえ将棋", "四七八将棋", 1, "illegal_action")
            self.assertEqual(snapshots, [expected, expected])

    def test_wcsc_distinct_replay_is_not_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official_dir = root / "official"
            live_dir = root / "live"
            official_dir.mkdir()
            live_dir.mkdir()
            official = (
                "V2.2\nN+257\nN-Kanade\n"
                "$EVENT:第35回世界コンピュータ将棋選手権一次予選2回戦\n"
                "$START_TIME:2025/05/03 11:22:06\n"
                "PI\n+\n+7776FU\n%TORYO\n")
            live_event = (
                "WCSC35+L2_45_2-900-5F+nigonana+Kanade+20250503113118")
            live = (
                f"V2.2\nN+nigonana\nN-Kanade\n$EVENT:{live_event}\n"
                "$START_TIME:2025/05/03 11:31:18\n"
                "PI\n+\n+2726FU\n%TORYO\n")
            (official_dir / "WCSC35-L2-nigonana-Kanade.csa").write_bytes(
                official.encode("cp932"))
            (live_dir / f"{live_event}.csa").write_text(
                live, encoding="utf-8")
            db_path = root / "test.db"

            ingest_folder(db_path, official_dir)
            ingest_folder(db_path, live_dir)
            conn = open_for_write(db_path)
            try:
                rows = conn.execute(
                    "SELECT event, source FROM games ORDER BY event").fetchall()
            finally:
                conn.close()
            self.assertEqual(len(rows), 2)
            self.assertEqual(sum(game_url(source, event) is not None
                                 for event, source in rows), 1)

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
            self.assertEqual(len(stats.error_files), 1)
            self.assertTrue(stats.error_files[0].startswith("bad.csa ("))
            self.assertEqual(stats.summary_ja(),
                             "確認 2件 / 追加 1局 / エラー 1件")
            conn = open_for_write(db_path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM games").fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "SELECT status FROM source_files WHERE path LIKE '%bad.csa'").fetchone()[0],
                    "error")
            finally:
                conn.close()

    def test_errors_from_an_older_parser_are_retried_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "test.db"
            folders = []
            for name in ("a", "b"):
                folder = root / name
                folder.mkdir()
                (folder / f"{name}.csa").write_text(
                    f"V2.2\n$EVENT:{name}\nPI\n+\n+7776FU\n%TORYO\n",
                    encoding="utf-8")
                folders.append(folder)
            # 旧パーサ時代の台帳: ファイルは変わっていないが error のまま
            conn = open_for_write(db_path)
            try:
                for folder in folders:
                    for path in folder.iterdir():
                        st = path.stat()
                        conn.execute(
                            "INSERT INTO source_files (path, file_size, "
                            "file_mtime_ns, status, detail, ingested_at) "
                            "VALUES (?,?,?,'error','old parser',"
                            "'2000-01-01 00:00:00')",
                            (str(path), st.st_size, st.st_mtime_ns))
                conn.commit()
            finally:
                conn.close()

            # 版が上がった後に別フォルダを先に取り込んでも、b も読み直される
            for folder in folders:
                with self.subTest(folder=folder.name):
                    self.assertEqual(ingest_folder(db_path, folder).added, 1)
            again = ingest_folder(db_path, folders[0])
            self.assertEqual((again.added, again.skipped_unchanged), (0, 1))

    def test_errors_from_the_current_parser_are_not_reread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "kifu"
            folder.mkdir()
            (folder / "bad.csa").write_text(
                "V2.2\nPI\n+\n+0055FU\n%TORYO\n", encoding="utf-8")
            db_path = root / "test.db"
            self.assertEqual(ingest_folder(db_path, folder).errors, 1)
            again = ingest_folder(db_path, folder)
            self.assertEqual((again.errors, again.skipped_unchanged), (0, 1))

    def test_sfen_rejects_rank_wider_than_nine_squares(self) -> None:
        with self.assertRaises(ValueError):
            Position().set_sfen("9P/9/9/9/9/9/9/9/9 b -")

    def test_standard_sfen_remains_accepted(self) -> None:
        pos = Position()
        pos.set_sfen("lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b -")
        self.assertEqual(pos.sfen_key_string(),
                         "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b -")

    def test_denryusen_dr7_main_event_uses_its_own_folder(self) -> None:
        # 第7回本戦は接頭辞 dr7prd のまま、フォルダだけ dr7_prd
        # (dr7_production は 404)。第6回までは drN_production のまま。
        event = ("dr7prd+buoy_blackbid420_dr7prdb1-9-bottom_4_yorkie_gokaku"
                 "-600-2F+yorkie+gokaku+20261115180044")
        self.assertEqual(detect_source(event), "denryusen")
        self.assertEqual(
            game_url("denryusen", event, ply=40),
            f"https://denryu-sen.jp/denryusen/dr7_prd/dist/#/{event}/40")
        old = "dr6prd+x_a_b-600-2F+a+b+20251206100000"
        self.assertEqual(
            game_url("denryusen", old),
            f"https://denryu-sen.jp/denryusen/dr6_production/dist/#/{old}")


if __name__ == "__main__":
    unittest.main()
