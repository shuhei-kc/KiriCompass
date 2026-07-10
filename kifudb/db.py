"""SQLite schema and connection helpers for the precedent database.

Design goals:
- concurrent read while updating: WAL journal, read-only connections for viewers
- minimal size: positions are stored only as 64-bit keys; per-game data is
  stored once in `games`; per-position rows carry integers only
- self-contained: every game stores its full move sequence, so the original
  kifu file is not needed for display, verification or link generation
"""

from __future__ import annotations

import sqlite3
import time as _time
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per game. `event` is the unique tournament/server game id
-- (e.g. the floodgate filename stem) and is the dedupe key.
CREATE TABLE IF NOT EXISTS games (
    game_id      INTEGER PRIMARY KEY,
    event        TEXT NOT NULL UNIQUE,
    source       TEXT NOT NULL,            -- floodgate / wcsc / denryusen / other
    started_at   TEXT,                     -- ISO 8601, sortable
    black_name   TEXT NOT NULL,            -- from kifu content (N+)
    white_name   TEXT NOT NULL,            -- from kifu content (N-)
    result       INTEGER,                  -- 0 draw, 1 black win, 2 white win, NULL none
    end_reason   TEXT NOT NULL,            -- normalized token, e.g. 'toryo'
    ply_count    INTEGER NOT NULL,
    initial_sfen TEXT,                     -- NULL = standard start position
    moves        BLOB NOT NULL             -- uint16 little-endian move codes
);
CREATE INDEX IF NOT EXISTS idx_games_started_at ON games(started_at);

-- Engine analysis per game (evaluations and principal variations).
-- Present only for games whose record contains analysis comments.
-- Encoding: see analysis.py (int16 evals; zlib-framed move16 PVs).
CREATE TABLE IF NOT EXISTS game_analysis (
    game_id INTEGER PRIMARY KEY,
    evals   BLOB NOT NULL,
    pvs     BLOB
);

-- One row per (position, game, ply). This is the precedent index.
-- sort_key (game start time, minutes since epoch UTC, 0 = unknown) is part
-- of the key so that "newest N precedents" is a backward index range scan:
-- no join-then-sort over every match, first query included.
CREATE TABLE IF NOT EXISTS position_games (
    position_key INTEGER NOT NULL,          -- 64-bit position hash (signed)
    sort_key     INTEGER NOT NULL,
    game_id      INTEGER NOT NULL,
    ply          INTEGER NOT NULL,          -- moves played to reach the position
    next_move    INTEGER NOT NULL,          -- move16 code, 0 = game ended here
    PRIMARY KEY (position_key, sort_key, game_id, ply)
) WITHOUT ROWID;

-- Aggregated candidate-move statistics, kept only for positions reached
-- by 2+ games; singletons are aggregated at query time from position_games.
CREATE TABLE IF NOT EXISTS position_stats (
    position_key INTEGER NOT NULL,
    next_move    INTEGER NOT NULL,
    game_count   INTEGER NOT NULL,
    black_wins   INTEGER NOT NULL,
    white_wins   INTEGER NOT NULL,
    draws        INTEGER NOT NULL,
    PRIMARY KEY (position_key, next_move)
) WITHOUT ROWID;

-- Ingestion ledger: enables incremental updates and re-checking of
-- files that were unfinished or unreadable on a previous run.
CREATE TABLE IF NOT EXISTS source_files (
    path          TEXT PRIMARY KEY,
    file_size     INTEGER NOT NULL,
    file_mtime_ns INTEGER NOT NULL,
    status     TEXT NOT NULL,               -- ok / unfinished / empty /
                                            -- duplicate / error / conflict(.sfen)
    detail     TEXT,
    game_id    INTEGER,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def open_for_write(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    # A large page cache keeps the position index B-tree in memory during
    # bulk ingestion; without it, random inserts degrade badly once the
    # index outgrows the default 2MB cache (especially on external drives).
    conn.execute("PRAGMA cache_size=-262144")   # 256MB
    conn.execute("PRAGMA temp_store=MEMORY")
    _migrate_if_needed(conn)
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def _migrate_if_needed(conn: sqlite3.Connection) -> None:
    """In-place migration of older databases (currently: v2 -> v3)."""
    import logging
    log = logging.getLogger("kifudb.db")
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return  # fresh database
    if row is None or int(row[0]) >= SCHEMA_VERSION:
        return
    version = int(row[0])
    if version == 2:
        log.info("migrating schema v2 -> v3 (rebuilding position index "
                 "with date sort key)...")
        started = _time.time()
        # 中断された前回の移行の残骸があれば片付ける (再実行を安全にする)
        conn.execute("DROP TABLE IF EXISTS position_games_v3")
        conn.commit()
        conn.execute("""
            CREATE TABLE position_games_v3 (
                position_key INTEGER NOT NULL,
                sort_key     INTEGER NOT NULL,
                game_id      INTEGER NOT NULL,
                ply          INTEGER NOT NULL,
                next_move    INTEGER NOT NULL,
                PRIMARY KEY (position_key, sort_key, game_id, ply)
            ) WITHOUT ROWID""")
        total = conn.execute("SELECT COUNT(*) FROM position_games").fetchone()[0]
        log.info("phase 1/3: sorting and inserting %d rows "
                 "(watch the -wal file grow)...", total)
        # INSERT〜メタ更新までは1トランザクション: どこで中断しても
        # ロールバックされ、DBはv2のまま無傷で残る。
        conn.execute("""
            INSERT INTO position_games_v3
                SELECT pg.position_key,
                       COALESCE(CAST(strftime('%s', g.started_at) AS INTEGER) / 60, 0),
                       pg.game_id, pg.ply, pg.next_move
                FROM position_games pg JOIN games g USING (game_id)
                ORDER BY 1, 2, 3, 4""")
        log.info("phase 2/3: swapping tables...")
        conn.execute("DROP TABLE position_games")
        conn.execute("ALTER TABLE position_games_v3 RENAME TO position_games")
        conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
        conn.commit()
        log.info("phase 3/3: VACUUM (reclaiming disk space; safe to "
                 "interrupt, the database is already migrated)...")
        conn.execute("VACUUM")
        log.info("migration to v3 complete in %.0fs", _time.time() - started)
    else:
        raise RuntimeError(
            f"unsupported schema version {version}; rebuild the database")


def open_read_only(db_path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: readers are used from GUI worker threads;
    # PrecedentReader serializes all access to a shared connection with an
    # RLock (query.py), so cross-thread use is safe.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0,
                           check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA cache_size=-65536")    # 64MB
    conn.execute("PRAGMA mmap_size=1073741824")  # mmap up to 1GB
    return conn
