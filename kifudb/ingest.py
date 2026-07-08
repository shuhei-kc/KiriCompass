"""Incremental kifu-folder ingestion into the precedent database."""

from __future__ import annotations

import array
import logging
import re
import sqlite3
import time
from pathlib import Path

from . import analysis, csa, kif
from .db import open_for_write

log = logging.getLogger("kifudb.ingest")

KIFU_SUFFIXES = {".csa", ".kif", ".kifu"}

_SOURCE_PATTERNS = [
    (re.compile(r"wdoor\+floodgate", re.I), "floodgate"),
    (re.compile(r"^wdoor\+", re.I), "wdoor"),
    (re.compile(r"^wcsc", re.I), "wcsc"),
    (re.compile(r"^dr\d", re.I), "denryusen"),
]


def detect_source(event_or_name: str) -> str:
    for pattern, name in _SOURCE_PATTERNS:
        if pattern.search(event_or_name):
            return name
    return "other"


class IngestStats:
    def __init__(self) -> None:
        self.scanned = 0
        self.added = 0
        self.skipped_unchanged = 0
        self.unfinished = 0
        self.empty = 0
        self.duplicates = 0
        self.errors = 0

    def summary(self) -> str:
        return (f"scanned={self.scanned} added={self.added} "
                f"unchanged={self.skipped_unchanged} unfinished={self.unfinished} "
                f"aborted={self.empty} duplicates={self.duplicates} "
                f"errors={self.errors}")


def ingest_folder(db_path: str | Path, folder: str | Path,
                  batch_size: int = 500,
                  pv_max_moves: int = 255,
                  progress=None) -> IngestStats:
    """Scan `folder` recursively and add finished games not yet in the DB.

    Safe to re-run: unchanged files are skipped via the source_files ledger,
    unfinished/errored files are retried once their size or mtime changes.
    """
    folder = Path(folder)
    conn = open_for_write(db_path)
    stats = IngestStats()

    files = sorted(p for p in folder.rglob("*")
                   if p.suffix.lower() in KIFU_SUFFIXES and p.is_file())
    total = len(files)
    log.info("found %d kifu files under %s", total, folder)

    ledger = {path: (size, mtime_ns) for path, size, mtime_ns in conn.execute(
        "SELECT path, file_size, file_mtime_ns FROM source_files")}

    touched_keys: set[int] = set()
    started = time.time()
    in_batch = 0

    for index, path in enumerate(files, 1):
        stats.scanned += 1
        st = path.stat()
        key = str(path)
        if ledger.get(key) == (st.st_size, st.st_mtime_ns):
            stats.skipped_unchanged += 1
            continue

        status, detail, game_id = _ingest_file(conn, path, touched_keys, stats,
                                               pv_max_moves)
        conn.execute(
            "INSERT OR REPLACE INTO source_files "
            "(path, file_size, file_mtime_ns, status, detail, game_id) "
            "VALUES (?,?,?,?,?,?)",
            (key, st.st_size, st.st_mtime_ns, status, detail, game_id))
        in_batch += 1

        if in_batch >= batch_size:
            _refresh_stats(conn, touched_keys)
            conn.commit()
            in_batch = 0
            if progress:
                progress(index, total)
            log.info("progress %d/%d (%s)", index, total, stats.summary())

    _refresh_stats(conn, touched_keys)
    conn.commit()
    conn.close()
    log.info("done in %.1fs: %s", time.time() - started, stats.summary())
    return stats


def _ingest_file(conn: sqlite3.Connection, path: Path,
                 touched_keys: set, stats: IngestStats,
                 pv_max_moves: int = 255):
    try:
        raw = path.read_bytes()
        if path.suffix.lower() in (".kif", ".kifu"):
            rec = kif.parse_kif(kif.decode_kif_bytes(raw), source_name=path.name)
        else:
            rec = csa.parse_csa(csa.decode_bytes(raw), source_name=path.name)
    except (csa.CsaParseError, OSError) as exc:
        stats.errors += 1
        log.warning("error: %s (%s)", path.name, exc)
        return "error", str(exc), None

    if not rec.finished:
        if not rec.moves:
            # Header-only record: the game never started (e.g. one side
            # failed to connect and the server closed the game).
            stats.empty += 1
            log.info("aborted game (no moves), skipped: %s", path.name)
            return "empty", "no moves", None
        stats.unfinished += 1
        log.info("no result recorded (%d moves), skipped: %s",
                 len(rec.moves), path.name)
        return "unfinished", f"{len(rec.moves)} moves, no result", None
    for warning in rec.parse_warnings:
        log.warning("%s: %s", path.name, warning)

    event = rec.event or path.stem
    cursor = conn.execute(
        "INSERT OR IGNORE INTO games "
        "(event, source, started_at, black_name, white_name, result, "
        " end_reason, ply_count, initial_sfen, moves) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (event, detect_source(event), rec.start_time, rec.black_name,
         rec.white_name, rec.result, rec.end_reason, len(rec.moves),
         rec.initial_sfen, array.array("H", rec.moves).tobytes()))
    if cursor.rowcount == 0:
        stats.duplicates += 1
        log.info("duplicate, skipped: %s", path.name)
        return "duplicate", None, None
    game_id = cursor.lastrowid

    if rec.has_analysis:
        pvs = ([pv[:pv_max_moves] for pv in rec.pvs] if pv_max_moves < 255
               else rec.pvs)
        conn.execute(
            "INSERT OR REPLACE INTO game_analysis VALUES (?,?,?)",
            (game_id, analysis.encode_evals(rec.evals),
             analysis.encode_pvs(pvs)))

    rows = []
    n_moves = len(rec.moves)
    for ply, position_key in enumerate(rec.sfen_keys):
        next_move = rec.moves[ply] if ply < n_moves else 0
        rows.append((position_key, game_id, ply, next_move))
        touched_keys.add(position_key)
    conn.executemany(
        "INSERT OR IGNORE INTO position_games VALUES (?,?,?,?)", rows)
    stats.added += 1
    return "ok", None, game_id


def _refresh_stats(conn: sqlite3.Connection, touched_keys: set) -> None:
    """Recompute stats for touched positions from position_games.

    Rows are kept only for positions reached by 2 or more games; singleton
    positions (the vast majority) are aggregated on the fly at query time.
    Recomputing from position_games keeps the table correct under
    incremental updates regardless of this pruning.
    """
    if not touched_keys:
        return
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS touched (k INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM touched")
    conn.executemany("INSERT OR IGNORE INTO touched VALUES (?)",
                     [(k,) for k in touched_keys])
    conn.execute("DELETE FROM position_stats "
                 "WHERE position_key IN (SELECT k FROM touched)")
    conn.execute(
        "INSERT INTO position_stats "
        "SELECT pg.position_key, pg.next_move, COUNT(*), "
        "       SUM(g.result IS 1), SUM(g.result IS 2), SUM(g.result IS 0) "
        "FROM position_games pg JOIN games g USING (game_id) "
        "WHERE pg.position_key IN "
        "      (SELECT position_key FROM position_games "
        "       WHERE position_key IN (SELECT k FROM touched) "
        "       GROUP BY position_key HAVING COUNT(*) >= 2) "
        "GROUP BY pg.position_key, pg.next_move")
    touched_keys.clear()
