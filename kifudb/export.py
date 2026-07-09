"""Reconstruct a CSA kifu file from the database.

The database stores each game's complete move sequence, so restoration is
exact move-for-move — including sennichite loops, since the sequence (not
the position index) is the source. What is and is not restorable:

restorable:
- 全指し手 (千日手の繰り返しも元の手順のまま)、初期局面 (駒落ち・任意局面)
- 対局者名 (棋譜内表記)、$EVENT、開始日時 (秒まで)
- 終局理由と勝敗 (%トークン + summary行として書き出し、再取り込みでも一致)
- 評価値・読み筋 (取り込み時に '**形式へ正規化されたもの)

not restorable (DBに保存していない):
- 消費時間 (T行)、レーティング行、Max_Moves等のヘッダーコメント
- フリーテキストのコメント、KIF固有ヘッダー (棋戦・場所・持ち時間など)
- 評価値の±32767超の原値 (クランプ済み)、255手を超える読み筋の後半
- 未終局として取り込み対象外になった棋譜
"""

from __future__ import annotations

import re

from .board import (BLACK, FU, KY, KE, GI, KI, KA, HI, OU, TO, NY, NK, NG,
                    UM, RY, Position, apply_move16, usi_to_move16)
from .query import GameDetail

CSA_CODE = {FU: "FU", KY: "KY", KE: "KE", GI: "GI", KI: "KI", KA: "KA",
            HI: "HI", OU: "OU", TO: "TO", NY: "NY", NK: "NK", NG: "NG",
            UM: "UM", RY: "RY"}
PROMOTE = {FU: TO, KY: NY, KE: NK, GI: NG, KA: UM, HI: RY}

REASON_TO_SPECIAL = {
    "toryo": "%TORYO", "time_up": "%TIME_UP", "sennichite": "%SENNICHITE",
    "oute_sennichite": "%OUTE_SENNICHITE", "jishogi": "%JISHOGI",
    "kachi": "%KACHI", "hikiwake": "%HIKIWAKE", "max_moves": "%MAX_MOVES",
    "chudan": "%CHUDAN", "matta": "%MATTA", "illegal_move": "%ILLEGAL_MOVE",
    "uchifuzume": "%UCHIFUZUME", "oute_kaihimore": "%OUTE_KAIHIMORE",
    "tsumi": "%TSUMI", "fuzumi": "%FUZUMI", "error": "%ERROR",
    "abnormal": "%ABNORMAL",
}


def _sq_csa(sq: int) -> str:
    return f"{sq // 9 + 1}{sq % 9 + 1}"


def _move_to_csa(pos: Position, code: int) -> str:
    """CSA token for `code` from `pos` (does not apply the move)."""
    sign = "+" if pos.turn == BLACK else "-"
    to = code & 0x7F
    frm = (code >> 7) & 0x7F
    if frm >= 81:
        return f"{sign}00{_sq_csa(to)}{CSA_CODE[frm - 81]}"
    piece = pos.board[frm]
    if piece is None:
        raise ValueError(f"no piece on {frm}")
    ptype = piece[1]
    if code & (1 << 14):
        ptype = PROMOTE.get(ptype, ptype)
    return f"{sign}{_sq_csa(frm)}{_sq_csa(to)}{CSA_CODE[ptype]}"


def _pv_to_csa(pos: Position, usi_moves: list[str]) -> list[str]:
    """CSA tokens for a PV; tolerant of tails that no longer apply."""
    scratch = pos.copy()
    tokens = []
    for usi in usi_moves:
        code = usi_to_move16(usi)
        if code is None:
            break
        try:
            tokens.append(_move_to_csa(scratch, code))
            apply_move16(scratch, code)
        except (ValueError, KeyError, TypeError):
            break
    return tokens


def _position_setup_lines(pos: Position) -> list[str]:
    lines = []
    for rank in range(9):
        cells = []
        for file_idx in range(8, -1, -1):
            piece = pos.board[file_idx * 9 + rank]
            if piece is None:
                cells.append(" * ")
            else:
                sign = "+" if piece[0] == BLACK else "-"
                cells.append(f"{sign}{CSA_CODE[piece[1]]}")
        lines.append(f"P{rank + 1}" + "".join(cells))
    for color, mark in ((0, "+"), (1, "-")):
        parts = "".join(f"00{CSA_CODE[t]}" * n
                        for t, n in sorted(pos.hands[color].items()) if n)
        if parts:
            lines.append(f"P{mark}{parts}")
    return lines


def game_to_csa(detail: GameDetail) -> str:
    """Rebuild a CSA record. Re-parsing the output with kifudb's own parser
    yields identical moves, result, end reason and analysis data."""
    pos = Position()
    if detail.initial_sfen:
        pos.set_sfen(detail.initial_sfen)
    else:
        pos.set_hirate()

    lines = ["V2.2"]
    lines.append(f"N+{detail.black_name}")
    lines.append(f"N-{detail.white_name}")
    if detail.event:
        lines.append(f"$EVENT:{detail.event}")
    if detail.started_at:
        lines.append(f"$START_TIME:{detail.started_at.replace('-', '/')}")
    if detail.initial_sfen:
        lines += _position_setup_lines(pos)
        lines.append("+" if pos.turn == BLACK else "-")
    else:
        lines.append("PI")
        lines.append("+")

    evals = detail.evals
    pvs = detail.pvs_usi

    def analysis_comment(slot: int) -> str | None:
        value = evals[slot] if slot < len(evals) else None
        pv = pvs[slot] if slot < len(pvs) else []
        if value is None and not pv:
            return None
        tokens = _pv_to_csa(pos, pv) if pv else []
        return f"'** {value if value is not None else 0}" + \
            ("" if not tokens else " " + " ".join(tokens))

    for index, usi in enumerate(detail.moves_usi):
        comment = analysis_comment(index)  # position before this move
        if comment:
            lines.append(comment)
        code = usi_to_move16(usi)
        lines.append(_move_to_csa(pos, code))
        apply_move16(pos, code)
    comment = analysis_comment(len(detail.moves_usi))
    if comment:
        lines.append(comment)

    special = REASON_TO_SPECIAL.get(detail.end_reason)
    if special:
        lines.append(special)

    # floodgate形式のsummary行: 再取り込み時に勝敗を厳密に復元するため。
    # (パーサーは%トークンのパリティ推定よりsummaryを優先する)
    if detail.result is not None:
        r_black, r_white = {1: ("win", "lose"), 2: ("lose", "win"),
                            0: ("draw", "draw")}[detail.result]
        lines.append(f"'summary:{detail.end_reason or 'unknown'}:"
                     f"{detail.black_name} {r_black}:"
                     f"{detail.white_name} {r_white}")
    return "\n".join(lines) + "\n"


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name)[:200]
