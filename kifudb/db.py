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
from pathlib import Path

SCHEMA_VERSION = 2

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
CREATE TABLE IF NOT EXISTS position_games (
    position_key INTEGER NOT NULL,          -- 64-bit position hash (signed)
    game_id      INTEGER NOT NULL,
    ply          INTEGER NOT NULL,          -- moves played to reach the position
    next_move    INTEGER NOT NULL,          -- move16 code, 0 = game ended here
    PRIMARY KEY (position_key, game_id, ply)
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
    status     TEXT NOT NULL,               -- ok / unfinished / duplicate / error
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
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def open_read_only(db_path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: readers are used from GUI worker threads;
    # SQLite objects are safe here because each reader serializes its use.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0,
                           check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA cache_size=-65536")    # 64MB
    conn.execute("PRAGMA mmap_size=1073741824")  # mmap up to 1GB
    return conn
