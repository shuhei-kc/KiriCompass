"""Read-side precedent lookup: sfen -> candidate moves + precedent games."""

from __future__ import annotations

import array
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .analysis import decode_evals, decode_pvs
from .board import move16_to_usi, normalize_sfen_main, position_key_from_sfen
from .db import open_read_only

# 前例一覧の1ページあたりの件数 (GUI・CLI・APIのデフォルトを一元管理)
DEFAULT_PAGE_SIZE = 1000

# DBに現れる既知の出典。これ以外は絞り込みで「その他」に分類される。
KNOWN_SOURCES = ("floodgate", "wcsc", "denryusen")

REASON_JA = {
    "toryo": "投了", "time_up": "時間切れ", "illegal_move": "反則負け",
    "illegal_action": "反則行為", "sennichite": "千日手",
    "oute_sennichite": "王手千日手", "jishogi": "持将棋",
    "kachi": "入玉宣言", "hikiwake": "引き分け", "max_moves": "最大手数",
    "chudan": "中断", "matta": "待った", "uchifuzume": "打ち歩詰め",
    "oute_kaihimore": "王手放置", "tsumi": "詰み", "fuzumi": "不詰",
    "error": "エラー", "abnormal": "異常終了",
}


import re as _re

# 電竜戦: event接頭辞 → ビューアURLの大会ID (フォルダ)。
# 接頭辞は不規則で規則化できないもの (獅子王戦・後援大会・実験対局や、
# production の別綴り prod/prd 等) は明示表で対応し、規則的なもの
# (drNprd/drNprod/drNtsec/drNhdM/drNsakura) は _DENRYU_TOURNAMENT_RES で解決する。
# 表は tools/download_denryusen.py が実ダウンロードから生成する
# data/denryusen_prefix_map.txt を基にした (最頻フォルダを採用)。
_DENRYU_PREFIX_MAP = {
    "shishio3": "dr2_exhi2",     # 獅子王戦3
    "dr2ex2": "dr2_exhi2",
    "dr2long": "dr2_exhi1",      # 長時間エキシビション
    "donou3": "dr4_patronage_do3",  # 後援大会
    "drjikken": "dr4_sakura",    # 実験対局
    "dr1t4": "dr1_production",
    "dr1tsec1": "dr2_tsec",
}
_DENRYU_TOURNAMENT_RES = [
    (_re.compile(r"^dr(\d+)pro?d"), r"dr\1_production"),   # drNprd / drNprod
    (_re.compile(r"^dr(\d+)tsec"), r"dr\1_tsec"),
    (_re.compile(r"^dr(\d+)hd(\d+)"), r"dr\1_hardware\2"),
    (_re.compile(r"^dr(\d+)sakura"), r"dr\1_sakura"),
]

# 大会を跨いで掲載された対局の例外。棋譜の実体が接頭辞の示す大会に無く
# (dr2_tsec/kifufiles は404)、掲載先の大会ビューアでしか開けないもの。
_DENRYU_EVENT_OVERRIDES = {
    "dr2tsec+buoy_wakis1_t-1_27_suisho98_suisho99-300-2F"
    "+suisho98+suisho99+20210720231132": "dr2_exhi1",
}


def _denryusen_tournament(event: str) -> str | None:
    """電竜戦 event から大会ID (URLパス) を解決する。不明なら None。"""
    override = _DENRYU_EVENT_OVERRIDES.get(event)
    if override:
        return override
    prefix = event.split("+", 1)[0]
    if prefix in _DENRYU_PREFIX_MAP:
        return _DENRYU_PREFIX_MAP[prefix]
    for pattern, replacement in _DENRYU_TOURNAMENT_RES:
        if pattern.match(prefix):
            return pattern.sub(replacement, prefix)
    return None

_WCSC_EVENT_RE = _re.compile(r"^(wcsc|wcso)(\d+)", _re.I)


def _wcsc_tournament(event: str) -> tuple[str, int] | None:
    """event先頭から (種別, 回次) を返す。種別は 'wcsc' か 'wcso'(オンライン)。

    WCSO1 は2020年のオンライン開催 (実質 第30回) で、URLスラッグ・ファイル名
    ともに 'wcso1' 系を使うため 'wcsc' とは別扱いにする。
    """
    m = _WCSC_EVENT_RE.match(event)
    if m:
        return m.group(1).lower(), int(m.group(2))
    # WCSC28 決勝の一部 (F1〜F3) は配信ファイル名から回次番号が抜けており
    # (例: WCSC_F1_APR_MCB)、この形式は当該大会にのみ存在する。番号を補う。
    # URL側は event をそのまま /kifu/<event>.html に使うので改名は不要。
    if _re.match(r"^wcsc_f\d", event, _re.I):
        return "wcsc", 28
    return None


def _wcsc_url(event: str) -> str | None:
    """WCSC/WCSO の棋譜ビューアURL。回次ごとに配信元・パスが異なる。

    - 36回〜         : https://www.computer-shogi.org/live/wcscNN/html/<name>.html
                       (name は '+' を '_' に置換したフル名)
    - 33〜35回        : http://live4.computer-shogi.org/wcscNN/html/<event>.html
                       (event はフル名 '+' 区切りのまま)
    - 28〜32回        : http://live4.computer-shogi.org/wcscNN/kifu/<event>.html
                       (event は配信元の短縮名 'WCSC32_F7_YAN_TNK_1' 等)
    - WCSO1 (第30回)  : http://live4.computer-shogi.org/wcso1/kifu/<event>.html
    - 27回以前        : 個別棋譜ページ無し (非対応)
    """
    t = _wcsc_tournament(event)
    if t is None:
        return None
    kind, num = t
    if kind == "wcso":
        return f"http://live4.computer-shogi.org/wcso{num}/kifu/{event}.html"
    if num >= 36:
        return (f"https://www.computer-shogi.org/live/wcsc{num}"
                f"/html/{event.replace('+', '_')}.html")
    if num >= 33:
        return f"http://live4.computer-shogi.org/wcsc{num}/html/{event}.html"
    if num >= 28:
        return f"http://live4.computer-shogi.org/wcsc{num}/kifu/{event}.html"
    return None


def game_url(source: str, event: str, started_at: str = "",
             ply: int | None = None) -> str | None:
    """Public game-viewer URL for a stored game, when one exists.

    - floodgate/wdoor: https://wdoor.c.u-tokyo.ac.jp/shogi/x/YYYY/MM/DD/<event>.html
    - 電竜戦: https://denryu-sen.jp/denryusen/<tournament>/dist/#/<event>[/<ply>]
      (the viewer accepts a move-number anchor; tournament id needs mapping,
      e.g. event prefix 'dr5prd' -> path 'dr5_production')
    - WCSC: http://live4.computer-shogi.org/wcsc<NN>/html/<event>.html
      (WCSC26+ on live4; WCSC17-24 on live2; WCSC16 and older have no
      per-game pages — archives live at www2.computer-shogi.org/kifu/)
    """
    if source in ("floodgate", "wdoor") and started_at:
        date_dir = started_at[:10].replace("-", "/")
        return f"https://wdoor.c.u-tokyo.ac.jp/shogi/x/{date_dir}/{event}.html"
    if source == "denryusen":
        tournament = _denryusen_tournament(event)
        if tournament is None:
            return None
        # 第1回本戦だけは SPA 以前の旧ビューア (単一HTML + '#<event>'、手数
        # アンカー無し)。新しい大会は dist/#/<event>/<ply> のSPAルート。
        # (dr1 は dist/ が 403、dist/denryusen_single.html が 200)
        if tournament.startswith("dr1_"):
            return (f"https://denryu-sen.jp/denryusen/{tournament}"
                    f"/dist/denryusen_single.html#{event}")
        anchor = f"/{ply}" if ply else ""
        return f"https://denryu-sen.jp/denryusen/{tournament}/dist/#/{event}{anchor}"
    if source == "wcsc":
        return _wcsc_url(event)
    return None


def tournament_label(source: str, event: str) -> str:
    """Short tournament id for display/post text.

    floodgate -> "floodgate", WCSC -> "WCSC35", 電竜戦 -> "dr6_production" 等。
    """
    if source in ("floodgate", "wdoor"):
        return "floodgate"
    prefix = event.split("+", 1)[0]
    if source == "denryusen":
        return _denryusen_tournament(event) or prefix
    if source == "wcsc":
        # 短縮名(WCSC32_F7_...)・WCSO1・フル名すべてから大会名を取り出す。
        t = _wcsc_tournament(event)
        if t:
            kind, num = t
            return f"WCSO{num}" if kind == "wcso" else f"WCSC{num}"
        return prefix
    return source


@dataclass
class Candidate:
    usi: str
    game_count: int
    black_wins: int
    white_wins: int
    draws: int


@dataclass
class Precedent:
    game_id: int
    event: str
    source: str
    started_at: str
    black_name: str
    white_name: str
    next_move_usi: str          # "" if the game ended at this position
    result: int | None
    end_reason: str
    ply: int
    ply_count: int
    sort_key: int = 0           # keyset pagination cursor (see precedents_page)
    rank: int = 0               # 現在の絞り込み条件での新しい順の通し番号

    @property
    def url(self) -> str | None:
        return game_url(self.source, self.event, self.started_at, self.ply)


class PrecedentReader:
    """Read handle with a persistent connection.

    Reuse one instance for consecutive queries (USI engine, GUI): keeping
    the connection open preserves SQLite's page cache and the mmap window,
    which avoids sporadic slow lookups from cold reads — noticeable when
    the database lives on an external drive.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self.conn = open_read_only(db_path)
        self._intervals: "list[tuple[str, int, int]] | None" = None
        self._intervals_max_gid: int | None = None

    def _source_intervals(self) -> "list[tuple[str, int, int]]":
        """出典 → game_id 連続区間 (島) の一覧。キャッシュし、取り込みで
        game_id が伸びたら再計算する。

        取り込みは出典ごとにまとめて走るため、game_id は出典ごとの連続区間に
        まとまる (島の数は取り込みバッチ数程度)。この区間表を使うと出典の
        絞り込みを position_games の主キー列 (game_id) だけで判定でき、
        games への結合なしの索引走査になる。"""
        max_gid = self.conn.execute(
            "SELECT MAX(game_id) FROM games").fetchone()[0]
        if self._intervals is None or self._intervals_max_gid != max_gid:
            self._intervals = self.conn.execute(
                "SELECT source, MIN(game_id), MAX(game_id) FROM "
                "(SELECT source, game_id, game_id - ROW_NUMBER() OVER "
                " (PARTITION BY source ORDER BY game_id) AS grp FROM games) "
                "GROUP BY source, grp").fetchall()
            self._intervals_max_gid = max_gid
        return self._intervals

    def _source_condition(self, sources: "set[str]") -> str:
        """有効出典の集合を pg.game_id の区間条件 (SQL断片) に変換する。"""
        intervals = self._source_intervals()
        known = [(s, lo, hi) for s, lo, hi in intervals if s in KNOWN_SOURCES]
        terms = [f"pg.game_id BETWEEN {lo} AND {hi}"
                 for s, lo, hi in known if s in sources]
        if "other" in sources:
            not_known = [f"pg.game_id NOT BETWEEN {lo} AND {hi}"
                         for _s, lo, hi in known]
            terms.append("(" + " AND ".join(not_known) + ")"
                         if not_known else "1")
        return "AND (" + (" OR ".join(terms) if terms else "0") + ")"

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PrecedentReader":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def lookup(self, sfen: str, max_precedents: int = DEFAULT_PAGE_SIZE):
        """Return (candidates, precedents, total_games) for a position."""
        sfen_main = normalize_sfen_main(sfen)
        key = position_key_from_sfen(sfen_main)
        candidates = [
            Candidate(move16_to_usi(m) if m else "(end)", c, b, w, d)
            for m, c, b, w, d in self.conn.execute(
                "SELECT next_move, game_count, black_wins, white_wins, draws "
                "FROM position_stats WHERE position_key = ? "
                "ORDER BY game_count DESC", (key,))]
        if not candidates:
            # Singleton positions carry no stats row; aggregate on the fly
            # (at most a handful of position_games rows by construction).
            candidates = [
                Candidate(move16_to_usi(m) if m else "(end)", c, b or 0, w or 0, d or 0)
                for m, c, b, w, d in self.conn.execute(
                    "SELECT pg.next_move, COUNT(*), SUM(g.result IS 1), "
                    "       SUM(g.result IS 2), SUM(g.result IS 0) "
                    "FROM position_games pg JOIN games g USING (game_id) "
                    "WHERE pg.position_key = ? GROUP BY pg.next_move "
                    "ORDER BY COUNT(*) DESC", (key,))]
        total_games = sum(c.game_count for c in candidates)

        precedents = self.precedents_page(sfen, limit=max_precedents)
        return candidates, precedents, total_games

    def confluence_counts(self, sfen: str, candidates) -> "dict[str, int]":
        """各候補手を指した後の局面に「別手順で合流してくる」出現数。

        戻り値: usi -> 合流数 = (その手を指した後の局面の総出現数)
                            - (この局面からその手を直接指した出現数)。
        千日手等で同一対局が同じ局面を複数回通ると、その回数ぶん数える
        (局面インデックスが (対局, 手数) 行単位のため)。「対局数」ではなく
        出現ベースの参考値であり、勝率統計には含めない。候補手を1手適用する
        だけで求まるので合法手生成は不要 (未出現の手は transposition_moves)。
        """
        from .board import Position, apply_move16, usi_to_move16
        pos = Position()
        pos.set_sfen(normalize_sfen_main(sfen))
        out: dict[str, int] = {}
        for c in candidates:
            if not c.usi or c.usi == "(end)":
                continue
            code = usi_to_move16(c.usi)
            if code is None:
                continue
            nxt = pos.copy()
            try:
                apply_move16(nxt, code)
            except (ValueError, KeyError, TypeError):
                continue
            key = nxt.position_key()
            total = self.conn.execute(
                "SELECT SUM(game_count) FROM position_stats "
                "WHERE position_key = ?", (key,)).fetchone()[0]
            if total is None:  # 集計行が無い = その局面は単独対局 (合流なし)
                total = self.conn.execute(
                    "SELECT COUNT(*) FROM position_games "
                    "WHERE position_key = ?", (key,)).fetchone()[0]
            out[c.usi] = max(total - c.game_count, 0)
        return out

    def transposition_moves(self, sfen: str, candidates) -> "list[tuple[str, int]]":
        """この局面で前例が無いが、指すと既存対局に合流する手を返す。

        戻り値: [(usi, 合流数), ...] 合流数>0 のみ、降順。
        擬似合法手 (board.Position.pseudo_legal_moves) を1手ずつ試し、到達局面に
        対局があるものを拾う。王手放置・打ち歩詰め等は未チェックだが、それらが
        作る局面には実対局が無く合流0になるため結果に混じらない。正確な合法手
        生成ではないので、大規模用途では要改修。
        """
        from .board import Position, apply_move16, move16_to_usi
        played = {c.usi for c in candidates}
        pos = Position()
        pos.set_sfen(normalize_sfen_main(sfen))
        out: list[tuple[str, int]] = []
        for code in pos.pseudo_legal_moves():
            usi = move16_to_usi(code)
            if usi in played:
                continue
            nxt = pos.copy()
            try:
                apply_move16(nxt, code)
            except (ValueError, KeyError, TypeError):
                continue
            key = nxt.position_key()
            total = self.conn.execute(
                "SELECT SUM(game_count) FROM position_stats "
                "WHERE position_key = ?", (key,)).fetchone()[0]
            if total is None:
                total = self.conn.execute(
                    "SELECT COUNT(*) FROM position_games "
                    "WHERE position_key = ?", (key,)).fetchone()[0]
            if total:
                out.append((usi, total))
        out.sort(key=lambda x: -x[1])
        return out

    def precedents_page(self, sfen: str, limit: int = DEFAULT_PAGE_SIZE,
                        before: tuple[int, int] | None = None,
                        sources: "set[str] | None" = None,
                        start_rank: int = 0) -> "list[Precedent]":
        """One page of precedents, newest first.

        The date lives inside the position_games primary key, so this is a
        backward index range scan: only `limit` rows are read and joined,
        with no sort step — the first (cold-cache) query on a position with
        a million precedents costs the same as any other.

        `before` = (sort_key, game_id) of the last row already shown
        (keyset pagination; both values are on the returned Precedent).

        `sources`: None で全出典。集合を渡すと出典で絞り込む。絞り込みは
        出典→game_id 連続区間の表 (_source_intervals) を介して position_games
        の主キー列だけで判定するので、結合なしの索引走査のまま — 絞り込んでも
        速度はほぼ変わらない。rank は「現在の絞り込み条件での」新しい順の
        通し番号 (絞り込み無しなら全体順位)。`start_rank` は続き取得時に
        前ページ最終行の rank を渡す。
        """
        key = position_key_from_sfen(normalize_sfen_main(sfen))
        condition, params = "", [key]
        if before is not None:
            condition = ("AND (pg.sort_key < ? OR "
                         "(pg.sort_key = ? AND pg.game_id < ?)) ")
            params += [before[0], before[0], before[1]]
        if sources is not None:
            try:
                condition += self._source_condition(sources)
            except sqlite3.OperationalError:
                # window関数の無い古いSQLite: 結合側で絞る (走査は遅くなる)
                names = ",".join(f"'{s}'" for s in sources
                                 if s in KNOWN_SOURCES)
                parts = [f"g.source IN ({names})"] if names else []
                if "other" in sources:
                    known = ",".join(f"'{s}'" for s in KNOWN_SOURCES)
                    parts.append(f"g.source NOT IN ({known})")
                condition += "AND (" + (" OR ".join(parts) or "0") + ")"
        rows = self.conn.execute(
            "SELECT g.game_id, g.event, g.source, g.started_at, "
            "       g.black_name, g.white_name, pg.next_move, "
            "       g.result, g.end_reason, pg.ply, g.ply_count, "
            "       pg.sort_key "
            "FROM position_games pg JOIN games g USING (game_id) "
            f"WHERE pg.position_key = ? {condition} "
            "ORDER BY pg.sort_key DESC, pg.game_id DESC "
            "LIMIT ?", params + [limit])
        return [
            Precedent(game_id=r[0], event=r[1], source=r[2],
                      started_at=r[3] or "", black_name=r[4], white_name=r[5],
                      next_move_usi=move16_to_usi(r[6]) if r[6] else "",
                      result=r[7], end_reason=r[8], ply=r[9], ply_count=r[10],
                      sort_key=r[11], rank=start_rank + i + 1)
            for i, r in enumerate(rows)]

    def get_source_path(self, game_id: int) -> str | None:
        """取り込み元の棋譜ファイルのパス (台帳に記録があれば)。"""
        row = self.conn.execute(
            "SELECT path FROM source_files WHERE game_id = ?",
            (game_id,)).fetchone()
        return row[0] if row else None

    def get_game(self, game_id: int) -> "GameDetail | None":
        row = self.conn.execute(
            "SELECT event, source, started_at, black_name, white_name, "
            "       result, end_reason, initial_sfen, moves "
            "FROM games WHERE game_id = ?", (game_id,)).fetchone()
        if row is None:
            return None
        moves = array.array("H")
        moves.frombytes(row[8])
        detail = GameDetail(
            game_id=game_id, event=row[0], source=row[1], started_at=row[2] or "",
            black_name=row[3], white_name=row[4], result=row[5],
            end_reason=row[6], initial_sfen=row[7],
            moves_usi=[move16_to_usi(m) for m in moves])
        analysis_row = self.conn.execute(
            "SELECT evals, pvs FROM game_analysis WHERE game_id = ?",
            (game_id,)).fetchone()
        if analysis_row:
            detail.evals = decode_evals(analysis_row[0])
            detail.pvs_usi = [[move16_to_usi(m) for m in pv]
                              for pv in decode_pvs(analysis_row[1])]
        return detail


def lookup(db_path: str | Path, sfen: str,
           max_precedents: int = DEFAULT_PAGE_SIZE):
    """One-shot variant of PrecedentReader.lookup (opens/closes per call)."""
    with PrecedentReader(db_path) as reader:
        return reader.lookup(sfen, max_precedents)


@dataclass
class GameDetail:
    game_id: int
    event: str
    source: str
    started_at: str
    black_name: str
    white_name: str
    result: int | None
    end_reason: str
    initial_sfen: str | None
    moves_usi: list[str] = field(default_factory=list)
    evals: list[int | None] = field(default_factory=list)   # per position slot
    pvs_usi: list[list[str]] = field(default_factory=list)  # per position slot


def get_game(db_path: str | Path, game_id: int) -> GameDetail | None:
    """One-shot variant of PrecedentReader.get_game (opens/closes per call)."""
    with PrecedentReader(db_path) as reader:
        return reader.get_game(game_id)


def format_report(sfen: str, candidates, precedents, total_games: int) -> str:
    """Plain-text report in the spirit of the KiriCompass precedent pane."""
    from .board import Position, usi_to_move16
    from .ki2 import move16_to_ki2

    position = Position()
    position.set_sfen(normalize_sfen_main(sfen))

    def ki2(usi: str) -> str:
        code = usi_to_move16(usi) if usi else None
        return move16_to_ki2(position, code) if code is not None else "(終局)"

    lines = [f"sfen {normalize_sfen_main(sfen)}",
             f"前例: {total_games}局", ""]
    if candidates:
        lines.append("No. 指し手        USI     出現   先手勝  後手勝  引分   勝率")
        for rank, c in enumerate(candidates, start=1):
            # 引き分けは後手勝ち扱いで先手勝率を算出する
            decided = c.black_wins + c.white_wins + c.draws
            rate = (f"{c.black_wins / decided * 100:5.1f}%" if decided else "   -  ")
            lines.append(f"{rank:>3} {ki2(c.usi) if c.usi != '(end)' else '(終局)':<12}"
                         f" {c.usi:<7} {c.game_count:>5} {c.black_wins:>7} "
                         f"{c.white_wins:>7} {c.draws:>5} {rate:>7}")
    else:
        lines.append("(前例なし)")
    lines.append("")
    if precedents:
        lines.append("No. 対局日      先手 / 後手                         "
                     "次の一手    結果  終局理由  URL")
        for rank, p in enumerate(precedents, start=1):
            result = {1: "先手勝", 2: "後手勝", 0: "引分"}.get(p.result, "不明")
            reason = REASON_JA.get(p.end_reason, p.end_reason)
            players = f"{p.black_name} / {p.white_name}"
            date = p.started_at[:10].replace("-", "/")
            lines.append(f"{rank:>3} {date:<11} {players:<35} "
                         f"{ki2(p.next_move_usi):<10} {result:<5} "
                         f"{reason:<5} {p.url or ''}")
    return "\n".join(lines) + "\n"
