"""Incremental kifu-folder ingestion into the precedent database."""

from __future__ import annotations

import array
import calendar
import hashlib
import logging
import re
import sqlite3
import time
from pathlib import Path

from . import analysis, csa, kif
from .db import open_for_write, resolve_db_path

log = logging.getLogger("kifudb.ingest")

KIFU_SUFFIXES = {".csa", ".kif", ".kifu"}

# 出典判定は「配布できる公開棋譜である」ことの証明として使う (公開/プライベート
# の振り分け) ため、接頭辞だけでなくサーバー・大会の命名構造まで要求する —
# 接頭辞一致だけでは私的なファイル名 (例 'dr2_研究メモ.kif') が偶然マッチして
# 公開DBへ紛れ込む余地がある。全1,080,482件の実eventで一致を検証済み。
# - floodgate:  wdoor+<棋戦>+<先手>+<後手>+<開始時刻14桁>。テスト対局室も
#               含めてすべて floodgate 扱い (アーカイブとURL規則が同一のため)
# - WCSC/WCSO:  wcscNN/wcsoNN + 区切り (+ か _)。WCSO は2020年オンライン開催
#               (=第30回)。例外: WCSC28決勝の回次欠落形式 (WCSC_F1_...)
# - 電竜戦:     <大会接頭辞>+...+<開始時刻14桁>。接頭辞は dr<数字> で始まる
#               もの (将来の大会名への余裕) か、既知の変則接頭辞
#               (獅子王戦・後援大会等 → query.py の解決表)
_TS_TAIL_RE = re.compile(r"\+\d{14}$")
_WCSC_EVENT_RE = re.compile(r"^wcs[co]\d+[+_]|^wcsc_f\d", re.I)
_DR_PREFIX_RE = re.compile(r"^dr\d", re.I)


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
    event = event_or_name
    if event.lower().startswith("wdoor+") and _TS_TAIL_RE.search(event):
        return "floodgate"
    if _WCSC_EVENT_RE.match(event):
        return "wcsc"
    if _TS_TAIL_RE.search(event):
        prefix = event.split("+", 1)[0]
        if _DR_PREFIX_RE.match(prefix):
            return "denryusen"
        # 接頭辞が 'dr\d' でない電竜戦 (獅子王戦=shishio3、後援大会=donou3、
        # 実験対局=drjikken 等) は、URL解決表で拾えれば denryusen とする。
        from .query import _denryusen_tournament
        if _denryusen_tournament(event):
            return "denryusen"
    return "other"


def _buoy_designated(event: str) -> "tuple[int, str | None]":
    """buoy対局の指定手順 (サーバー上で事前に並べられた手順) を event名から得る。

    shogi-server の buoy対局は「開始手数 N」を対局名に含む:
        <大会>+buoy_<局面名>_<N>_<先手>_<後手>-<持時間>-<f>+<先手>+<後手>+<時刻>
    N手目開始 = 指定手順は N-1 手。この手順は対局エンジンが選んだ手ではない
    (TSECの指定局面・本戦の入札手順等) ため、統計から分離する根拠になる。
    戻り値: (指定手数 N-1, グループキー = buoy名のNまで)。buoyでない・Nが
    無い・N=1 (平手開始) は (0, None)。
    実DBのbuoy全10,090局で対局者名アンカーの不一致ゼロ、354グループの
    実測共通手順がすべて N-1 以上 (=Nより手前で分岐した例ゼロ) を確認済み。"""
    parts = event.split("+")
    if len(parts) < 5 or not parts[1].startswith("buoy_"):
        return 0, None
    buoy, black, white = parts[1], parts[2], parts[3]
    idx = buoy.rfind(f"_{black}_{white}-")
    head = buoy[:idx] if idx > 0 else buoy
    for seg in reversed(head.split("_")):
        if seg.isdigit():
            n = int(seg)
            if n >= 2:
                return n - 1, head
            return 0, None
    return 0, None


def _ensure_designated_game(conn: sqlite3.Connection, event: str, head: str,
                            buoy_ply: int, rec, source: str,
                            touched_keys: set) -> None:
    """指定手順を「擬似対局」として1回だけ登録・索引する。

    .sfen の課題局面 (sfen_ingest.py) と同型: 同じ指定局面を戦う全対局の
    共通手順は、この擬似対局1行だけが背負う。前例一覧では終局理由
    「指定局面」の行として見え、勝敗は持たない (result NULL) ので勝率にも
    入らない。event はグループキー由来で INSERT OR IGNORE により重複しない。"""
    pseudo_event = f"{event.split('+', 1)[0]}+{head}#指定局面"
    keys = rec.sfen_keys[:buoy_ply + 1]
    # 指定手順自身のハンドシェイク (初期局面に戻る前置き) はさらに除外。
    # 手順全体が初期局面に戻るだけなら、索引すべき区間が無いので登録しない。
    p_start = 0
    for ply in range(len(keys) - 1, 0, -1):
        if keys[ply] == keys[0]:
            p_start = ply
            break
    if p_start >= buoy_ply:
        return
    cursor = conn.execute(
        "INSERT OR IGNORE INTO games (event, source, started_at, black_name, "
        "white_name, result, end_reason, ply_count, initial_sfen, moves) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pseudo_event, source, rec.start_time, head[len("buoy_"):],
         "(指定手順)", None, "designated", buoy_ply, rec.initial_sfen,
         array.array("H", rec.moves[:buoy_ply]).tobytes()))
    if cursor.rowcount == 0:
        return  # 同じ指定局面の別対局が登録済み
    game_id = cursor.lastrowid
    sort_key = date_sort_key(rec.start_time)
    rows = []
    for ply in range(p_start, buoy_ply + 1):
        next_move = rec.moves[ply] if ply < buoy_ply else 0
        rows.append((keys[ply], sort_key, game_id, ply, next_move))
        touched_keys.add(keys[ply])
    conn.executemany(
        "INSERT OR IGNORE INTO position_games VALUES (?,?,?,?,?)", rows)


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
    # 取り込み先を必ずログに残す (素の名前は data/ に解決される)
    log.info("database: %s", resolve_db_path(db_path))
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
    source = detect_source(event)
    if source == "other":
        # 私的棋譜の event はファイル名語幹で、公開棋譜と違い一意性の保証が
        # ない (別フォルダの「対局1.kif」同士が別対局、はありがち)。指し手
        # 内容のハッシュを重複排除キーに含め、同名別対局は両方登録し、
        # 同一内容の複製だけを弾く。ハッシュは内容から決定的に決まるので、
        # DBから復元した棋譜の再取り込みでも同じ event になり二重登録しない。
        content = (array.array("H", rec.moves).tobytes()
                   + (rec.initial_sfen or "").encode())
        suffix = "#" + hashlib.blake2b(content, digest_size=4).hexdigest()
        if not event.endswith(suffix):
            event += suffix
    cursor = conn.execute(
        "INSERT OR IGNORE INTO games "
        "(event, source, started_at, black_name, white_name, result, "
        " end_reason, ply_count, initial_sfen, moves) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (event, source, rec.start_time, rec.black_name,
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
    n_moves = len(rec.moves)
    start_ply = 0
    for ply in range(len(keys) - 1, 0, -1):
        if keys[ply] == keys[0]:
            start_ply = ply
            break
    # buoy対局 (TSEC指定局面・本戦入札等) は、サーバーが事前に並べた指定手順
    # を対局本体の索引から外し、代わりに「指定手順」擬似対局として1回だけ
    # 索引する — 指定局面までの手はこの対局のエンジンが選んだ手ではなく、
    # そのまま数えると序盤統計が汚れるため (.sfenの課題局面と同じ扱い)。
    buoy_ply, buoy_head = _buoy_designated(event)
    if start_ply < buoy_ply < n_moves:
        _ensure_designated_game(conn, event, buoy_head, buoy_ply, rec,
                                source, touched_keys)
        start_ply = buoy_ply
    rows = []
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
