"""Read-side precedent lookup: sfen -> candidate moves + precedent games."""

from __future__ import annotations

import array
from dataclasses import dataclass, field
from pathlib import Path

from .analysis import decode_evals, decode_pvs
from .board import move16_to_usi, normalize_sfen_main, position_key_from_sfen
from .db import open_read_only

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

_DENRYU_TOURNAMENT_RES = [
    (_re.compile(r"^dr(\d+)prd$"), r"dr\1_production"),
    (_re.compile(r"^dr(\d+)hd(\d+)$"), r"dr\1_hardware\2"),
]


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
        prefix = event.split("+", 1)[0]
        tournament = prefix
        for pattern, replacement in _DENRYU_TOURNAMENT_RES:
            if pattern.match(prefix):
                tournament = pattern.sub(replacement, prefix)
                break
        anchor = f"/{ply}" if ply else ""
        return f"https://denryu-sen.jp/denryusen/{tournament}/dist/#/{event}{anchor}"
    if source == "wcsc" and "+" in event:
        m = _re.match(r"^wcsc(\d+)", event, _re.I)
        if m:
            number = int(m.group(1))
            host = "live4" if number >= 25 else "live2" if number >= 17 else None
            if host:
                return (f"http://{host}.computer-shogi.org/wcsc{number}"
                        f"/html/{event}.html")
    return None


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

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PrecedentReader":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def lookup(self, sfen: str, max_precedents: int = 500):
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

        precedents = [
            Precedent(game_id=r[0], event=r[1], source=r[2], started_at=r[3] or "",
                      black_name=r[4], white_name=r[5],
                      next_move_usi=move16_to_usi(r[6]) if r[6] else "",
                      result=r[7], end_reason=r[8], ply=r[9], ply_count=r[10])
            for r in self.conn.execute(
                "SELECT g.game_id, g.event, g.source, g.started_at, "
                "       g.black_name, g.white_name, pg.next_move, "
                "       g.result, g.end_reason, pg.ply, g.ply_count "
                "FROM position_games pg JOIN games g USING (game_id) "
                "WHERE pg.position_key = ? "
                "ORDER BY g.started_at DESC LIMIT ?", (key, max_precedents))]
        return candidates, precedents, total_games

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


def lookup(db_path: str | Path, sfen: str, max_precedents: int = 500):
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
            decided = c.black_wins + c.white_wins
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
