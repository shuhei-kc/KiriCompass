"""Incremental kifu-folder ingestion into the precedent database."""

from __future__ import annotations

import array
import calendar
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
    # wdoorサーバー上の対局はテスト対局室 (wdoor+test-... 等) も含めて
    # すべて floodgate 扱いにする (アーカイブとURL規則が同一のため)。
    (re.compile(r"^wdoor\+", re.I), "floodgate"),
    # WCSC本戦(wcscNN)に加え、2020年オンライン開催のWCSO(=第30回)も
    # WCSC系として扱う。
    (re.compile(r"^wcs[co]", re.I), "wcsc"),
    (re.compile(r"^dr\d", re.I), "denryusen"),
]


def date_sort_key(started_at: str) -> int:
    """'YYYY-MM-DD HH:MM:SS' -> minutes since epoch (UTC), 0 if unknown.

    Must stay consistent with the SQL expression used in the v2->v3
    migration: strftime('%s', started_at) / 60.
    """
    try:
        return calendar.timegm(time.strptime(started_at,
                                             "%Y-%m-%d %H:%M:%S")) // 60
    except (ValueError, TypeError):
        return 0


def detect_source(event_or_name: str) -> str:
    for pattern, name in _SOURCE_PATTERNS:
        if pattern.search(event_or_name):
            return name
    # 電竜戦には接頭辞が 'dr\d' でないもの (獅子王戦=shishio3、後援大会=donou3、
    # 実験対局=drjikken 等) があるので、URL解決表で拾えれば denryusen とする。
    from .query import _denryusen_tournament
    if _denryusen_tournament(event_or_name):
        return "denryusen"
    return "other"


# WCSC/WCSO の event から「大会」と「回戦」を取り出す (日付復元用)。
_WCSC_EDITION_RE = re.compile(r"^(WCS[CO]\d+)", re.I)
_WCSC_ROUND_RE = re.compile(r"^WCS[CO]\d+[_+]([A-Za-z]+\d+)", re.I)


def _wcsc_round_key(event: str) -> str | None:
    m = _WCSC_ROUND_RE.match(event)
    return m.group(1).upper() if m else None


def _recover_wcsc_date(conn: sqlite3.Connection, event: str) -> str | None:
    """時刻を持たない大会対局の「日付」を同大会の他対局から復元する。

    完全にデータ駆動: 同じ棋譜集合を取り込めば誰でも同じ結果になる。
    優先順位は 同大会・同回戦 → 同大会・同フェーズ(L*/F*等) → 同大会 の
    最小日付。時刻は捏造せず、呼び出し側で 00:00:00 を付す。
    """
    m = _WCSC_EDITION_RE.match(event)
    if not m:
        return None
    edition = m.group(1)
    rows = conn.execute(
        "SELECT event, started_at FROM games "
        "WHERE source='wcsc' AND event LIKE ? "
        "AND started_at IS NOT NULL AND started_at != ''",
        (edition + "%",)).fetchall()
    if not rows:
        return None
    round_key = _wcsc_round_key(event)
    same_round = [sa[:10] for ev, sa in rows if round_key and _wcsc_round_key(ev) == round_key]
    if same_round:
        return min(same_round)
    phase = round_key[0] if round_key else None
    same_phase = [sa[:10] for ev, sa in rows
                  if phase and (_wcsc_round_key(ev) or "")[:1] == phase]
    if same_phase:
        return min(same_phase)
    return min(sa[:10] for _ev, sa in rows)


def _backfill_missing_dates(conn: sqlite3.Connection) -> int:
    """開始日時が無い大会対局に、復元した日付 + 00:00:00 を補う。

    大会棋譜には稀に時刻情報を持たない対局がある (WCSCの一部予選リーグ等)。
    日付不明のままだと sort_key=0 で時系列ソートの最古に沈むため、日付だけを
    復元して他対局と正しい順序で並ぶようにする。時刻(00:00:00)は「不明」を表す
    センチネルで、表示もしない。sort_key は date_sort_key と同じ規則で
    position_games にも反映する。"""
    undated = conn.execute(
        "SELECT game_id, event, initial_sfen, moves FROM games "
        "WHERE source='wcsc' AND (started_at IS NULL OR started_at = '')"
    ).fetchall()
    fixed = 0
    for game_id, event, initial_sfen, moves_blob in undated:
        date = _recover_wcsc_date(conn, event)
        if not date:
            continue
        started_at = f"{date} 00:00:00"
        conn.execute("UPDATE games SET started_at = ? WHERE game_id = ?",
                     (started_at, game_id))
        _update_game_sort_keys(conn, game_id, initial_sfen, moves_blob,
                               date_sort_key(started_at))
        fixed += 1
    return fixed


def _update_game_sort_keys(conn: sqlite3.Connection, game_id: int,
                           initial_sfen: str | None, moves_blob: bytes,
                           sort_key: int) -> None:
    """1対局分の position_games.sort_key を主キー参照だけで更新する。

    position_games には game_id 単独の索引が無く、game_id 条件だけの UPDATE は
    全表スキャンになる (統合DBでは1億行超で数分かかる)。対局の指し手を再生して
    各手数の position_key を復元し、完全な主キー (position_key, 旧sort_key=0,
    game_id, ply) で1行ずつ狙い撃ちする。索引から除外された手数 (ハンドシェイク
    等) の行は存在せず、0行更新になるだけで無害。"""
    from .board import Position, apply_move16
    pos = Position()
    try:
        if initial_sfen:
            pos.set_sfen(initial_sfen)
        else:
            pos.set_hirate()
        keys = [pos.position_key()]
        moves = array.array("H")
        moves.frombytes(moves_blob)
        for code in moves:
            apply_move16(pos, code)
            keys.append(pos.position_key())
    except (ValueError, KeyError, TypeError) as exc:
        # 再生に失敗しても取り込み全体は止めない (sort_key=0 のまま残る)。
        log.warning("sort_key backfill: replay failed for game %d: %s",
                    game_id, exc)
        return
    conn.executemany(
        "UPDATE position_games SET sort_key = ? "
        "WHERE position_key = ? AND sort_key = 0 AND game_id = ? AND ply = ?",
        [(sort_key, key, game_id, ply) for ply, key in enumerate(keys)])


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
                  progress=None, file_filter=None) -> IngestStats:
    """Scan `folder` recursively and add finished games not yet in the DB.

    Safe to re-run: unchanged files are skipped via the source_files ledger,
    unfinished/errored files are retried once their size or mtime changes.
    `file_filter(path)` を渡すと True のファイルだけを対象にする
    (公開/プライベートの振り分け取り込みに使う)。
    """
    folder = Path(folder)
    conn = open_for_write(db_path)
    stats = IngestStats()

    # 旧バージョンが 'wdoor' と分類したレコードを floodgate に正規化する。
    migrated = conn.execute(
        "UPDATE games SET source='floodgate' WHERE source='wdoor'").rowcount
    if migrated:
        conn.commit()
        log.info("migrated %d games: source 'wdoor' -> 'floodgate'", migrated)

    files = sorted(p for p in folder.rglob("*")
                   if p.suffix.lower() in KIFU_SUFFIXES and p.is_file()
                   and (file_filter is None or file_filter(p)))
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
    backfilled = _backfill_missing_dates(conn)
    if backfilled:
        log.info("recovered dates for %d undated tournament game(s) "
                 "from sibling games", backfilled)
    conn.commit()
    # WALを本体へ畳み、-wal/-shm をほぼ空に戻す。残った -wal をユーザが
    # 手で消して直近の取り込みを失う事故を防ぐ。読み取り接続が同時に
    # 開いているときは畳める分だけ畳む (ベストエフォート、次回に持ち越し)。
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass
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

    # 電竜戦buoy等の「初期局面に戻る前置き手順(ハンドシェイク)」を索引から除外する。
    # 例: 開始4手が玉の往復 (5i5h 5a5b 5h5i 5b5a) で平手に戻り、その後に実戦。
    # 初期局面へ完全に戻るのは実質この種の無効手順だけなので、初期局面が
    # 再出現する最後の ply までを前置きとみなし、そこから索引する。ply番号は
    # 元のまま (moves 本体も完全保持) なのでビューアの手数アンカーはズレない。
    keys = rec.sfen_keys
    start_ply = 0
    for ply in range(len(keys) - 1, 0, -1):
        if keys[ply] == keys[0]:
            start_ply = ply
            break
    rows = []
    n_moves = len(rec.moves)
    sort_key = date_sort_key(rec.start_time)
    for ply in range(start_ply, len(keys)):
        position_key = keys[ply]
        next_move = rec.moves[ply] if ply < n_moves else 0
        rows.append((position_key, sort_key, game_id, ply, next_move))
        touched_keys.add(position_key)
    conn.executemany(
        "INSERT OR IGNORE INTO position_games VALUES (?,?,?,?,?)", rows)
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
