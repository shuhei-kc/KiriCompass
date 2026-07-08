"""KI2 (棋譜表記) move formatting: ☗７六歩 / ☖同　銀 / ☗５八金左 など.

日本将棋連盟の表記規則に従い、同じ地点へ動ける同種の駒がある場合のみ
左/右/直/上/引/寄 を付け、盤上の駒でも到達できる地点への駒打ちにのみ
打 を付ける。成れる手を成らなかった場合は 不成 を付ける。
方向修飾のアルゴリズムは tsshogi (ShogiHome) の実装と同じ挙動になるよう
移植し、照合テスト済み。依存ライブラリなし。
"""

from __future__ import annotations

from .board import (BLACK, FU, KY, KE, GI, KI, KA, HI, OU, TO, NY, NK, NG, UM,
                    RY, PIECE_KANJI, Position, apply_move16)

_FILE_ZEN = "０１２３４５６７８９"
_RANK_KAN = "〇一二三四五六七八九"

PROMOTABLE = {FU, KY, KE, GI, KA, HI}

_GOLD_STEPS = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (0, 1)]
_ORTHO = [(0, -1), (0, 1), (-1, 0), (1, 0)]
_DIAG = [(-1, -1), (1, -1), (-1, 1), (1, 1)]

# (dfile, drank) from Black's point of view; drank -1 = forward.
_STEPS = {
    FU: [(0, -1)],
    KE: [(-1, -2), (1, -2)],
    GI: [(-1, -1), (0, -1), (1, -1), (-1, 1), (1, 1)],
    KI: _GOLD_STEPS, TO: _GOLD_STEPS, NY: _GOLD_STEPS,
    NK: _GOLD_STEPS, NG: _GOLD_STEPS,
    OU: _ORTHO + _DIAG,
    UM: _ORTHO,   # + diagonal slides
    RY: _DIAG,    # + orthogonal slides
}
_SLIDES = {KY: [(0, -1)], KA: _DIAG, HI: _ORTHO, UM: _DIAG, RY: _ORTHO}


def _can_reach(pos: Position, frm: int, to: int) -> bool:
    """Pseudo-legal reachability of the piece on `frm` to square `to`."""
    color, ptype = pos.board[frm]
    sign = 1 if color == BLACK else -1
    f, r = frm // 9, frm % 9
    for df, dr in _STEPS.get(ptype, ()):
        if f + df == to // 9 and r + dr * sign == to % 9:
            return True
    for df, dr in _SLIDES.get(ptype, ()):
        nf, nr = f + df, r + dr * sign
        while 0 <= nf <= 8 and 0 <= nr <= 8:
            sq = nf * 9 + nr
            if sq == to:
                return True
            if pos.board[sq] is not None:
                break
            nf, nr = nf + df, nr + dr * sign
    return False


def _same_piece_attackers(pos: Position, to: int, color: int, ptype: int,
                          exclude: int = -1) -> list[int]:
    result = []
    for sq in range(81):
        if sq == exclude:
            continue
        piece = pos.board[sq]
        if piece is not None and piece == (color, ptype) and _can_reach(pos, sq, to):
            result.append(sq)
    return result


def _direction(from_sq: int, to_sq: int, color: int) -> tuple[int, int]:
    """(vdir, hdir) normalized to the mover's perspective.

    vdir: -1 = forward (上), +1 = backward (引), 0 = sideways.
    hdir: -1 = moving toward the mover's right, +1 = toward the left, 0 = straight.
    """
    mult = 1 if color == BLACK else -1
    ndf = (to_sq // 9 - from_sq // 9) * mult
    ndr = (to_sq % 9 - from_sq % 9) * mult
    return (-1 if ndr < 0 else 1 if ndr > 0 else 0,
            1 if ndf > 0 else -1 if ndf < 0 else 0)


def _direction_modifier(pos: Position, frm: int, to: int,
                        color: int, ptype: int) -> str:
    others = _same_piece_attackers(pos, to, color, ptype, exclude=frm)
    if not others:
        return ""
    my_v, my_h = _direction(frm, to, color)
    other_dirs = [_direction(sq, to, color) for sq in others]
    # 垂直方向が同じ駒とは水平(左/右/直)で、水平方向が同じ駒とは
    # 垂直(上/引/寄)で区別する (tsshogi と同じ判定)。
    h_conflict = [oh for ov, oh in other_dirs if ov == my_v]
    v_conflict = [ov for ov, oh in other_dirs if oh == my_h]

    result = ""
    no_vertical = False
    if h_conflict:
        if ptype in (UM, RY):
            # 馬・竜は最大2枚なので「直」を使わず左右で区別する。
            if my_h == 1 or (my_h == 0 and h_conflict[0] == -1):
                result += "右"
            elif my_h == -1 or (my_h == 0 and h_conflict[0] == 1):
                result += "左"
        else:
            if my_h == 1:      # 左へ動く = 右側の駒
                result += "右"
            elif my_h == 0:
                result += "直"
                no_vertical = True
            elif my_h == -1:   # 右へ動く = 左側の駒
                result += "左"
    if not no_vertical and (v_conflict or not h_conflict):
        result += {1: "引", 0: "寄", -1: "上"}[my_v]
    return result


def _in_promotion_zone(sq: int, color: int) -> bool:
    rank = sq % 9
    return rank <= 2 if color == BLACK else rank >= 6


def move16_to_ki2(pos: Position, code: int, prev_to: int | None = None) -> str:
    """KI2 notation for a move from `pos` (the position before the move)."""
    to = code & 0x7F
    frm = (code >> 7) & 0x7F
    color = pos.turn
    mark = "☗" if color == BLACK else "☖"
    dest = "同　" if prev_to == to else \
        f"{_FILE_ZEN[to // 9 + 1]}{_RANK_KAN[to % 9 + 1]}"

    if frm >= 81:  # drop
        ptype = frm - 81
        suffix = "打" if _same_piece_attackers(pos, to, color, ptype) else ""
        return f"{mark}{dest}{PIECE_KANJI[ptype]}{suffix}"

    piece = pos.board[frm]
    if piece is None:
        return f"{mark}{dest}?"
    ptype = piece[1]
    result = mark + dest + PIECE_KANJI[ptype]
    result += _direction_modifier(pos, frm, to, color, ptype)
    if code & (1 << 14):
        result += "成"
    elif ptype in PROMOTABLE and (
            _in_promotion_zone(frm, color) or _in_promotion_zone(to, color)):
        result += "不成"
    return result


def format_pv_ki2(pos: Position, codes: list[int],
                  prev_to: int | None = None) -> str:
    """Render a sequence of moves in KI2 notation (同 handled in-sequence)."""
    scratch = pos.copy()
    parts = []
    for code in codes:
        parts.append(move16_to_ki2(scratch, code, prev_to))
        try:
            apply_move16(scratch, code)
        except (ValueError, KeyError, TypeError):
            break
        prev_to = code & 0x7F
    return " ".join(parts)
