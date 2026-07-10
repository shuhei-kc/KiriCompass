"""たややん掘り (連続自己対局 .sfen) の取り込みとバッチ管理。

1ファイル = 1バッチ (連続対局のひとまとまり)。各行が1局で、
「(startpos | sfen <sfen>) moves <usi...> <resign|rep_draw>」の形式。
勝敗は resign の手数パリティで判定し、rep_draw は千日手引き分け。
それ以外の終端トークンは警告してその行をスキップする。

設計 (前例DBとの関係):
- 実対局の前例DB (csa.db) とは **別の専用DB** に入れる。自己対局の出現数は
  探索の訪問頻度であって実対局の統計と意味が異なる上、候補手統計は出典
  フィルタの外 (DB全体の合算) なので、混ぜると統計が汚れる。バッチ削除
  機能の誤爆半径を隔離する目的もある。
- バッチ内の全局は課題局面まで同一手順を辿る。そのまま索引すると課題局面
  までの一本道に全局数が乗って無意味なので、共通接頭辞は「課題局面までの
  手順」という擬似対局 (event '<バッチ>#課題局面', end_reason 'task') として
  1回だけ索引し、各対局は共通接頭辞の終端 (=課題局面) から索引する。
  moves 本体は全局とも完全に保持され、棋譜表示はいつでも全手順を出せる。
- 同名ファイルの追記は自動で追加分のみ取り込む。既読部分が書き換わって
  いたら取り込まず conflict として記録し (警告)、バッチ削除→再取り込みで
  仕切り直す (=登録後の .sfen 編集はこの手順で反映する)。
"""

from __future__ import annotations

import array
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .board import Position, apply_move16, normalize_sfen_main, usi_to_move16
from .db import open_for_write, open_read_only
from .ingest import _refresh_stats, date_sort_key

RESULT_DRAW, RESULT_BLACK, RESULT_WHITE = 0, 1, 2

_STEM_DATE_RE = re.compile(r"(\d{14})")
_TERMINALS = {"resign", "rep_draw"}
TASK_SUFFIX = "#課題局面"          # 擬似対局のevent接尾辞 (連番の代わりの共通文言)


@dataclass
class ParsedGame:
    lineno: int
    initial_sfen: str | None       # None = 平手 (startpos)
    usi_moves: list[str]
    result: int | None
    reason: str


@dataclass
class SfenScan:
    """取り込み前プレビュー用のファイル概要。"""
    path: str
    stem: str
    date_str: str                  # 'YYYY-MM-DD HH:MM:SS' or ''
    total_lines: int
    games: int
    skipped: list[str]             # 行単位の警告
    prefix_len: int                # 共通接頭辞 (課題局面までの手数)


@dataclass
class SfenIngestResult:
    added: int = 0
    skipped: list[str] = field(default_factory=list)
    conflict: bool = False
    unchanged: bool = False
    rebuilt: bool = False          # 追記で課題局面が動き、バッチを再構築した
    prefix_len: int = 0
    error: str = ""                # 取り込みを拒否した理由 (同名別内容など)

    def summary(self) -> str:
        if self.error:
            return self.error
        if self.conflict:
            return "conflict: 既読部分が変更されています (削除→再取り込みで反映)"
        if self.unchanged:
            return "変更なし"
        parts = []
        if self.rebuilt:
            parts.append("課題局面の位置が変わったため再構築")
        parts.append(f"追加 {self.added}局")
        if self.prefix_len:
            parts.append(f"課題局面 {self.prefix_len}手目")
        if self.skipped:
            parts.append(f"警告スキップ {len(self.skipped)}行")
        return " / ".join(parts)


def date_from_name(path: str | Path) -> str:
    m = _STEM_DATE_RE.search(Path(path).stem)
    if not m:
        return ""
    ts = m.group(1)
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}"


def _read_lines(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines()


def _lines_hash(lines: list[bytes]) -> str:
    return hashlib.sha256(b"\n".join(lines)).hexdigest()


def _parse_line(raw: bytes, lineno: int) -> ParsedGame | str:
    """1行を解析する。問題があれば警告文字列を返す (その行はスキップ)。"""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return f"{lineno}行目: 空行"
    head, sep, tail = text.partition(" moves ")
    if not sep:
        return f"{lineno}行目: 'moves' 区切りがありません"
    head = head.strip()
    if head == "startpos":
        initial = None
    elif head.startswith("sfen "):
        initial = normalize_sfen_main(head[5:].strip())
    else:
        return f"{lineno}行目: 初期局面の指定が不正です ({head[:20]})"
    tokens = tail.split()
    if not tokens:
        return f"{lineno}行目: 指し手がありません"
    terminal = tokens[-1]
    if terminal not in _TERMINALS:
        return (f"{lineno}行目: 終局トークンが不正です ({terminal!r}) "
                "- resign/rep_draw のみ対応")
    usi_moves = tokens[:-1]
    if not usi_moves:
        return f"{lineno}行目: 指し手がありません"
    if terminal == "rep_draw":
        result, reason = RESULT_DRAW, "sennichite"
    else:  # resign: 最後に指した側の勝ち (奇数手なら先手)
        result = RESULT_BLACK if len(usi_moves) % 2 == 1 else RESULT_WHITE
        reason = "toryo"
    return ParsedGame(lineno, initial, usi_moves, result, reason)


def _parse_lines(lines: list[bytes], start_lineno: int = 1):
    games: list[ParsedGame] = []
    skipped: list[str] = []
    for offset, raw in enumerate(lines):
        parsed = _parse_line(raw, start_lineno + offset)
        if isinstance(parsed, str):
            if raw.strip():  # 完全な空行は黙って読み飛ばす
                skipped.append(parsed)
        else:
            games.append(parsed)
    return games, skipped


def _common_prefix_len(games: list[ParsedGame]) -> int:
    """バッチの共通接頭辞 (課題局面までの手数)。2局未満・初期局面不一致は0。"""
    if len(games) < 2:
        return 0
    if len({g.initial_sfen for g in games}) != 1:
        return 0
    first = games[0].usi_moves
    prefix = len(first)
    for g in games[1:]:
        limit = min(prefix, len(g.usi_moves))
        i = 0
        while i < limit and g.usi_moves[i] == first[i]:
            i += 1
        prefix = i
        if prefix == 0:
            break
    return prefix


def _replay(initial_sfen: str | None, usi_moves: list[str]):
    """usi手順を再生して (move16列, 局面キー列) を返す。不正手はValueError。"""
    pos = Position()
    if initial_sfen:
        pos.set_sfen(initial_sfen)
    else:
        pos.set_hirate()
    keys = [pos.position_key()]
    moves16 = []
    for usi in usi_moves:
        code = usi_to_move16(usi)
        if code is None:
            raise ValueError(f"不正なUSI手: {usi}")
        apply_move16(pos, code)
        moves16.append(code)
        keys.append(pos.position_key())
    return moves16, keys


def scan_file(path: str | Path) -> SfenScan:
    """取り込み前のプレビュー情報を集める (盤面再生なしで軽量)。"""
    path = Path(path)
    lines = _read_lines(path)
    games, skipped = _parse_lines(lines)
    return SfenScan(path=str(path), stem=path.stem,
                    date_str=date_from_name(path),
                    total_lines=len(lines), games=len(games),
                    skipped=skipped, prefix_len=_common_prefix_len(games))


def _detail_row(conn: sqlite3.Connection, path: Path):
    row = conn.execute("SELECT status, detail FROM source_files WHERE path=?",
                       (str(path),)).fetchone()
    if row is None:
        return None, None
    try:
        detail = json.loads(row[1])
        if detail.get("type") != "sfen":
            return None, None
    except (TypeError, ValueError):
        return None, None
    return row[0], detail


def _insert_game(conn, event, started_at, label, initial_sfen, moves16,
                 keys16, start_ply, result, reason, touched) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO games (event, source, started_at, black_name, "
        "white_name, result, end_reason, ply_count, initial_sfen, moves) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (event, "sfen", started_at, label, label, result, reason,
         len(moves16), initial_sfen, array.array("H", moves16).tobytes()))
    if cursor.rowcount == 0:
        return False
    game_id = cursor.lastrowid
    sort_key = date_sort_key(started_at)
    rows = []
    n = len(moves16)
    for ply in range(min(start_ply, n), len(keys16)):
        next_move = moves16[ply] if ply < n else 0
        rows.append((keys16[ply], sort_key, game_id, ply, next_move))
        touched.add(keys16[ply])
    conn.executemany(
        "INSERT OR IGNORE INTO position_games VALUES (?,?,?,?,?)", rows)
    return True


def _find_batch_by_stem(conn: sqlite3.Connection, stem: str):
    """同じバッチ名 (stem) の台帳行を探す。(path, detail) か None。

    台帳はフルパス鍵・対局は '<stem>#' 鍵なので、同名ファイルを別の場所から
    取り込むと両者の1:1対応が壊れる。取り込み前にここで検出する。"""
    for p, detail_json in conn.execute(
            "SELECT path, detail FROM source_files WHERE path LIKE ?",
            (f"%{stem}.sfen",)):
        if Path(p).stem != stem:
            continue
        try:
            detail = json.loads(detail_json)
        except (TypeError, ValueError):
            continue
        if detail.get("type") == "sfen":
            return p, detail
    return None


def ingest_file(db_path: str | Path, path: str | Path, label: str = "",
                date_str: str | None = None,
                log_line=None) -> SfenIngestResult:
    """1ファイル(=1バッチ)を取り込む。既知ファイルは追記分のみ自動追加。

    label: 対局者名として保存する自由記述 (エンジン名・深さ等)。空なら
    ファイル名 (stem)。date_str: 'YYYY-MM-DD HH:MM:SS'。None ならファイル名
    から取得 (取れなければ日付なし)。追記時は初回登録時の値を使い続ける。
    """
    path = Path(path)
    result = SfenIngestResult()

    def say(msg: str) -> None:
        if log_line:
            log_line(msg)

    lines = _read_lines(path)
    conn = open_for_write(db_path)
    try:
        status, detail = _detail_row(conn, path)
        if detail is None:
            # 同名バッチが別パスから登録済みか (ファイル移動 / 同名別物の検出)
            moved = _find_batch_by_stem(conn, path.stem)
            if moved is not None:
                old_path, old_detail = moved
                known = old_detail["lines"]
                if (len(lines) >= known
                        and _lines_hash(lines[:known]) == old_detail["sha256"]):
                    # 既読部分が同一 → ファイル移動とみなし台帳を付け替える
                    conn.execute("UPDATE source_files SET path=? WHERE path=?",
                                 (str(path), old_path))
                    detail = old_detail
                    say(f"[{path.stem}] ファイルの移動を検出: "
                        f"{old_path} → {path}")
                else:
                    result.conflict = True
                    result.error = (
                        f"同名バッチ ({path.stem}) が別の内容で登録済みです"
                        f" ({old_path})。別の掘りなら .sfen をリネームするか、"
                        "旧バッチを削除してから取り込んでください")
                    say(f"[{path.stem}] {result.error}")
                    return result
        fresh = True   # 新規/再構築 = 擬似対局から入れ直す
        if detail is not None:
            # --- 既知バッチ: 追記チェック (既読部分のハッシュを常に検証) ---
            known = detail["lines"]
            if (len(lines) < known
                    or _lines_hash(lines[:known]) != detail["sha256"]):
                conn.execute(
                    "UPDATE source_files SET status='conflict' WHERE path=?",
                    (str(path),))
                conn.commit()
                result.conflict = True
                say(f"[{path.stem}] {result.summary()}")
                return result
            if len(lines) == known:
                result.unchanged = True
                return result
            label = detail.get("label") or path.stem
            date_str = detail.get("date", "")
            old_games, _ = _parse_lines(lines[:known])  # 警告は初回に報告済み
            new_games, result.skipped = _parse_lines(lines[known:], known + 1)
            prefix_len = _common_prefix_len(old_games + new_games)
            if prefix_len != detail.get("prefix", 0):
                # 追記で課題局面の位置が変わった: より手前の分岐が判明したか、
                # 初回が1局のみで接頭辞を検出できていなかったケース。既読部分
                # はハッシュ検証済みなので、ファイルを正としてバッチ全体を
                # 同一トランザクション内で作り直す (中断はロールバック)。
                _delete_batch_rows(conn, path.stem)
                result.rebuilt = True
                new_games = old_games + new_games
            else:
                fresh = False
        else:
            # --- 新規バッチ ---
            new_games, result.skipped = _parse_lines(lines)
            label = label or path.stem
            if date_str is None:
                date_str = date_from_name(path)
            prefix_len = _common_prefix_len(new_games)

        if fresh and prefix_len and new_games:
            # 課題局面までの共通手順を擬似対局として1回だけ索引する
            rep = new_games[0]
            moves16, keys16 = _replay(rep.initial_sfen,
                                      rep.usi_moves[:prefix_len])
            task_touched: set[int] = set()
            _insert_game(conn, f"{path.stem}{TASK_SUFFIX}", date_str,
                         label, rep.initial_sfen, moves16, keys16, 0,
                         None, "task", task_touched)
            _refresh_stats(conn, task_touched)

        touched = set()
        for game in new_games:
            try:
                moves16, keys16 = _replay(game.initial_sfen, game.usi_moves)
            except (ValueError, KeyError, TypeError) as exc:
                result.skipped.append(f"{game.lineno}行目: 再生失敗 ({exc})")
                continue
            if _insert_game(conn, f"{path.stem}#L{game.lineno:05d}", date_str,
                            label, game.initial_sfen, moves16, keys16,
                            prefix_len, game.result, game.reason, touched):
                result.added += 1
        _refresh_stats(conn, touched)

        st = path.stat()
        conn.execute(
            "INSERT OR REPLACE INTO source_files "
            "(path, file_size, file_mtime_ns, status, detail) VALUES (?,?,?,?,?)",
            (str(path), st.st_size, st.st_mtime_ns, "ok", json.dumps({
                "type": "sfen", "lines": len(lines),
                "sha256": _lines_hash(lines), "prefix": prefix_len,
                "label": label, "date": date_str}, ensure_ascii=False)))
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        result.prefix_len = prefix_len
        for msg in result.skipped:
            say(f"[{path.stem}] 警告: {msg}")
        say(f"[{path.stem}] {result.summary()}")
        return result
    finally:
        conn.close()


def list_batches(db_path: str | Path) -> list[dict]:
    """登録済みバッチの一覧 (GUI表示用)。"""
    out = []
    conn = open_read_only(db_path)
    try:
        for path, status, detail_json in conn.execute(
                "SELECT path, status, detail FROM source_files"):
            try:
                detail = json.loads(detail_json)
            except (TypeError, ValueError):
                continue
            if detail.get("type") != "sfen":
                continue
            stem = Path(path).stem
            games = conn.execute(
                "SELECT COUNT(*) FROM games WHERE event LIKE ? AND event != ?",
                (f"{stem}#%", f"{stem}{TASK_SUFFIX}")).fetchone()[0]
            out.append({"path": path, "stem": stem, "status": status,
                        "label": detail.get("label", ""),
                        "date": (detail.get("date") or "")[:10],
                        "games": games, "prefix": detail.get("prefix", 0)})
    finally:
        conn.close()
    return sorted(out, key=lambda b: b["date"], reverse=True)


def _delete_batch_rows(conn: sqlite3.Connection, stem: str) -> int:
    """バッチの games / position_games 行を削除する (台帳行は残す)。

    position_games は各対局の moves を再生して完全な主キーで狙い撃ちする
    (日付バックフィルと同じ手法)。commit は呼び出し側が行う。"""
    games = conn.execute(
        "SELECT game_id, started_at, initial_sfen, moves FROM games "
        "WHERE event LIKE ?", (f"{stem}#%",)).fetchall()
    touched: set[int] = set()
    for game_id, started_at, initial_sfen, moves_blob in games:
        moves = array.array("H")
        moves.frombytes(moves_blob)
        pos = Position()
        if initial_sfen:
            pos.set_sfen(initial_sfen)
        else:
            pos.set_hirate()
        keys = [pos.position_key()]
        for code in moves:
            apply_move16(pos, code)
            keys.append(pos.position_key())
        sort_key = date_sort_key(started_at or "")
        conn.executemany(
            "DELETE FROM position_games WHERE position_key=? AND "
            "sort_key=? AND game_id=? AND ply=?",
            [(key, sort_key, game_id, ply) for ply, key in enumerate(keys)])
        touched.update(keys)
    _refresh_stats(conn, touched)
    conn.execute("DELETE FROM games WHERE event LIKE ?", (f"{stem}#%",))
    return len(games)


def delete_batch(db_path: str | Path, path: str | Path,
                 log_line=None) -> int:
    """バッチ (擬似対局含む) をDBから完全に削除する。戻り値は削除対局数。

    台帳行も消すので、再取り込みが可能になる (= .sfen を編集して入れ直す
    手順)。全体を1トランザクションで行う。"""
    path = Path(path)
    conn = open_for_write(db_path)
    try:
        deleted = _delete_batch_rows(conn, path.stem)
        conn.execute("DELETE FROM source_files WHERE path=?", (str(path),))
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        if log_line:
            log_line(f"[{path.stem}] バッチ削除: {deleted}件 (擬似対局含む)")
        return deleted
    finally:
        conn.close()
