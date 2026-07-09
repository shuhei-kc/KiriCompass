"""Minimal shogi position replay for kifu ingestion.

Pure-Python fallback backend. In production, the cshogi backend
(backend_cshogi.py) is preferred for speed; this module keeps the tool
working with zero native dependencies and defines the reference
semantics for position keys and 16-bit move encoding.

Square index: sq = file_index * 9 + rank_index
  file_index 0..8 = files 1..9 (right to left)
  rank_index 0..8 = ranks a..i (top to bottom)
This matches cshogi's square numbering.

Move encoding (16 bit, cshogi-compatible):
  bits 0-6  : to square (0..80)
  bits 7-13 : from square (0..80), or 81 + hand_piece for drops
  bit 14    : promotion flag
"""

from __future__ import annotations

import hashlib

BLACK, WHITE = 0, 1

# Piece type ids. 0..7 basic, 8..13 promoted.
FU, KY, KE, GI, KI, KA, HI, OU, TO, NY, NK, NG, UM, RY = range(14)

CSA_PIECE = {
    "FU": FU, "KY": KY, "KE": KE, "GI": GI, "KI": KI, "KA": KA, "HI": HI,
    "OU": OU, "TO": TO, "NY": NY, "NK": NK, "NG": NG, "UM": UM, "RY": RY,
}
SFEN_LETTER = ["P", "L", "N", "S", "G", "B", "R", "K",
               "+P", "+L", "+N", "+S", "+B", "+R"]
# Hand piece order used by cshogi (HPAWN..HROOK) == type ids 0..6.
HAND_SFEN_ORDER = [HI, KA, KI, GI, KE, KY, FU]  # sfen output order: R B G S N L P

HIRATE_BOARD_SFEN = "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL"

# Full piece set (type -> count) for hirate, used for P+/P- "AL".
FULL_SET = {FU: 18, KY: 4, KE: 4, GI: 4, KI: 4, KA: 2, HI: 2, OU: 2}

# --- 移動オフセット (先手視点; dr<0 が前方=上。後手は dr を反転) -----------
# (df, dr): file_index差, rank_index差。sq = file*9 + rank。
_GOLD = ((0, -1), (1, -1), (-1, -1), (1, 0), (-1, 0), (0, 1))
_SILVER = ((0, -1), (1, -1), (-1, -1), (1, 1), (-1, 1))
_KING = tuple((df, dr) for df in (-1, 0, 1) for dr in (-1, 0, 1)
              if (df, dr) != (0, 0))
_KNIGHT = ((1, -2), (-1, -2))
_PAWN = ((0, -1),)
_LANCE = ((0, -1),)
_BISHOP = ((1, 1), (1, -1), (-1, 1), (-1, -1))
_ROOK = ((0, 1), (0, -1), (1, 0), (-1, 0))
# 1マスだけ動く成分 (歩や桂は成り駒=金の動き; 馬=飛の直進1歩, 龍=角の斜め1歩)
_STEP_OFFSETS = {
    FU: _PAWN, KE: _KNIGHT, GI: _SILVER, KI: _GOLD, OU: _KING,
    TO: _GOLD, NY: _GOLD, NK: _GOLD, NG: _GOLD, UM: _ROOK, RY: _BISHOP,
}
# 走り駒の方向
_SLIDE_OFFSETS = {KY: _LANCE, KA: _BISHOP, HI: _ROOK, UM: _BISHOP, RY: _ROOK}
_PROMOTABLE = frozenset((FU, KY, KE, GI, KA, HI))


def unpromote(t: int) -> int:
    if t < TO:
        return t
    if t == UM:
        return KA
    if t == RY:
        return HI
    return t - 8


class Position:
    """A shogi position that can apply CSA-style moves (no legality check)."""

    __slots__ = ("board", "hands", "turn")

    def __init__(self) -> None:
        self.board: list[tuple[int, int] | None] = [None] * 81
        self.hands = ({t: 0 for t in range(7)}, {t: 0 for t in range(7)})
        self.turn = BLACK

    def copy(self) -> "Position":
        other = Position.__new__(Position)
        other.board = self.board.copy()
        other.hands = (self.hands[0].copy(), self.hands[1].copy())
        other.turn = self.turn
        return other

    # -- setup -------------------------------------------------------------

    def set_hirate(self) -> None:
        self.board = [None] * 81
        for color, back_rank, pawn_rank in ((WHITE, 0, 2), (BLACK, 8, 6)):
            order = [KY, KE, GI, KI, OU, KI, GI, KE, KY]
            for file_idx in range(9):
                self.board[file_idx * 9 + back_rank] = (color, order[file_idx])
                self.board[file_idx * 9 + pawn_rank] = (color, FU)
        # bishops and rooks
        self.board[7 * 9 + 1] = (WHITE, HI)  # 82
        self.board[1 * 9 + 1] = (WHITE, KA)  # 22
        self.board[1 * 9 + 7] = (BLACK, HI)  # 28
        self.board[7 * 9 + 7] = (BLACK, KA)  # 88
        self.hands = ({t: 0 for t in range(7)}, {t: 0 for t in range(7)})
        self.turn = BLACK

    def set_sfen(self, sfen_main: str) -> None:
        """Load a position from '<board> <turn> <hands>' (ply ignored)."""
        parts = sfen_main.split()
        if len(parts) < 2:
            raise ValueError(f"invalid sfen: {sfen_main}")
        board_str, turn = parts[0], parts[1]
        hands_str = parts[2] if len(parts) > 2 else "-"
        letter_to_type = {SFEN_LETTER[t]: t for t in range(14)}

        self.board = [None] * 81
        rows = board_str.split("/")
        if len(rows) != 9:
            raise ValueError(f"invalid sfen board: {board_str}")
        for rank, row in enumerate(rows):
            file_idx = 8
            promoted = False
            for ch in row:
                if ch == "+":
                    promoted = True
                    continue
                if ch.isdigit():
                    file_idx -= int(ch)
                    continue
                letter = ("+" if promoted else "") + ch.upper()
                t = letter_to_type.get(letter)
                if t is None:
                    raise ValueError(f"invalid sfen piece: {ch}")
                color = BLACK if ch.isupper() else WHITE
                self.board[file_idx * 9 + rank] = (color, t)
                file_idx -= 1
                promoted = False

        self.hands = ({t: 0 for t in range(7)}, {t: 0 for t in range(7)})
        if hands_str != "-":
            count = 0
            for ch in hands_str:
                if ch.isdigit():
                    count = count * 10 + int(ch)
                    continue
                t = letter_to_type.get(ch.upper())
                if t is None or t > 6:
                    raise ValueError(f"invalid sfen hand: {ch}")
                color = BLACK if ch.isupper() else WHITE
                self.hands[color][t] += count or 1
                count = 0
        self.turn = BLACK if turn == "b" else WHITE

    # -- CSA move application ---------------------------------------------

    def push_csa(self, color: int, frm: int, to: int, ptype: int) -> int:
        """Apply a CSA move; returns the 16-bit move code.

        frm/to are 0-based square indexes; frm == -1 means drop.
        ptype is the piece type *after* the move (CSA semantics).
        """
        captured = self.board[to]
        if captured is not None:
            self.hands[color][unpromote(captured[1])] += 1
        if frm < 0:  # drop
            self.hands[color][ptype] -= 1
            self.board[to] = (color, ptype)
            code = to | ((81 + ptype) << 7)
        else:
            moving = self.board[frm]
            if moving is None or moving[0] != color:
                raise ValueError(f"no piece of color {color} on square {frm}")
            promote = ptype != moving[1]
            if promote and unpromote(ptype) != moving[1]:
                raise ValueError("inconsistent promotion in CSA move")
            self.board[frm] = None
            self.board[to] = (color, ptype)
            code = to | (frm << 7) | (int(promote) << 14)
        self.turn = color ^ 1
        return code

    # -- output ------------------------------------------------------------

    def board_sfen(self) -> str:
        rows = []
        for rank in range(9):
            row, empties = [], 0
            for file_idx in range(8, -1, -1):
                piece = self.board[file_idx * 9 + rank]
                if piece is None:
                    empties += 1
                    continue
                if empties:
                    row.append(str(empties))
                    empties = 0
                letter = SFEN_LETTER[piece[1]]
                row.append(letter if piece[0] == BLACK else letter.lower())
            if empties:
                row.append(str(empties))
            rows.append("".join(row))
        return "/".join(rows)

    def hands_sfen(self) -> str:
        parts = []
        for color in (BLACK, WHITE):
            for t in HAND_SFEN_ORDER:
                n = self.hands[color][t]
                if n == 0:
                    continue
                letter = SFEN_LETTER[t]
                if color == WHITE:
                    letter = letter.lower()
                parts.append((str(n) if n > 1 else "") + letter)
        return "".join(parts) or "-"

    def sfen_key_string(self) -> str:
        """Canonical '<board> <turn> <hands>' string (ply intentionally omitted)."""
        turn = "b" if self.turn == BLACK else "w"
        return f"{self.board_sfen()} {turn} {self.hands_sfen()}"

    def position_key(self) -> int:
        return position_key_from_sfen(self.sfen_key_string())

    # -- 擬似合法手生成 ----------------------------------------------------
    # 注意: これは「擬似合法手」生成であり、真の合法手生成ではない。駒の動き
    # (二歩・行き所のない駒・成りの可否) だけを守り、王手放置(自玉が取られる手)
    # や打ち歩詰めはチェックしない。合流数の計算では、そうした反則手が作る局面
    # には実対局が存在せず合流0になるため無害。正確性が要る用途や大規模サイトへ
    # の転用時は、本物の合法手生成に差し替えること。

    def _emit_move(self, moves, frm, to, ptype, promo_zone, last_rank,
                   knight_ban) -> None:
        to_rank = to % 9
        from_rank = frm % 9
        can_promo = (ptype in _PROMOTABLE
                     and (from_rank in promo_zone or to_rank in promo_zone))
        must_promo = ((ptype in (FU, KY) and to_rank == last_rank)
                      or (ptype == KE and to_rank in knight_ban))
        if not must_promo:
            moves.append(to | (frm << 7))
        if can_promo:
            moves.append(to | (frm << 7) | (1 << 14))

    def pseudo_legal_moves(self) -> "list[int]":
        """手番側の擬似合法手を move16 のリストで返す (真の合法手ではない)。"""
        color = self.turn
        black = color == BLACK
        promo_zone = (0, 1, 2) if black else (6, 7, 8)
        last_rank = 0 if black else 8
        knight_ban = (0, 1) if black else (7, 8)
        moves: list[int] = []

        for sq in range(81):
            piece = self.board[sq]
            if piece is None or piece[0] != color:
                continue
            ptype = piece[1]
            f0, r0 = sq // 9, sq % 9
            for df, dr in _SLIDE_OFFSETS.get(ptype, ()):
                if not black:
                    dr = -dr
                f, r = f0 + df, r0 + dr
                while 0 <= f <= 8 and 0 <= r <= 8:
                    tp = self.board[f * 9 + r]
                    if tp is not None and tp[0] == color:
                        break
                    self._emit_move(moves, sq, f * 9 + r, ptype, promo_zone,
                                    last_rank, knight_ban)
                    if tp is not None:
                        break
                    f += df
                    r += dr
            for df, dr in _STEP_OFFSETS.get(ptype, ()):
                if not black:
                    dr = -dr
                f, r = f0 + df, r0 + dr
                if 0 <= f <= 8 and 0 <= r <= 8:
                    tp = self.board[f * 9 + r]
                    if tp is not None and tp[0] == color:
                        continue
                    self._emit_move(moves, sq, f * 9 + r, ptype, promo_zone,
                                    last_rank, knight_ban)

        # 持ち駒を打つ (二歩・行き所のない駒だけ避ける)
        hand = self.hands[color]
        pawn_files = {s // 9 for s in range(81)
                      if self.board[s] is not None
                      and self.board[s][0] == color and self.board[s][1] == FU}
        for t in range(7):
            if hand[t] == 0:
                continue
            for sq in range(81):
                if self.board[sq] is not None:
                    continue
                r = sq % 9
                if t == FU and (r == last_rank or sq // 9 in pawn_files):
                    continue
                if t == KY and r == last_rank:
                    continue
                if t == KE and r in knight_ban:
                    continue
                moves.append(sq | ((81 + t) << 7))
        return moves


def position_key_from_sfen(sfen_main: str) -> int:
    """64-bit position key from a canonical sfen main part (signed for SQLite)."""
    digest = hashlib.blake2b(sfen_main.encode(), digest_size=8).digest()
    value = int.from_bytes(digest, "little")
    return value - (1 << 64) if value >= (1 << 63) else value


def normalize_sfen_main(sfen: str) -> str:
    """Drop the move counter and normalize whitespace of a user-supplied sfen."""
    parts = sfen.split()
    if parts and parts[0] in ("position", "sfen"):
        parts = parts[1:]
    if parts == ["startpos"] or not parts:
        return f"{HIRATE_BOARD_SFEN} b -"
    board = parts[0]
    turn = parts[1] if len(parts) > 1 else "b"
    hands = parts[2] if len(parts) > 2 else "-"
    return f"{board} {turn} {hands}"


_USI_MOVE_RE = None


def usi_to_move16(token: str) -> int | None:
    """Convert one USI move token to a 16-bit code; None if not a USI move."""
    global _USI_MOVE_RE
    if _USI_MOVE_RE is None:
        import re
        _USI_MOVE_RE = re.compile(r"^(?:([PLNSGBR])\*([1-9])([a-i])"
                                  r"|([1-9])([a-i])([1-9])([a-i])(\+)?)$")
    m = _USI_MOVE_RE.match(token)
    if not m:
        return None
    if m.group(1):
        to_sq = (int(m.group(2)) - 1) * 9 + (ord(m.group(3)) - ord("a"))
        return to_sq | ((81 + "PLNSGBR".index(m.group(1))) << 7)
    frm = (int(m.group(4)) - 1) * 9 + (ord(m.group(5)) - ord("a"))
    to_sq = (int(m.group(6)) - 1) * 9 + (ord(m.group(7)) - ord("a"))
    return to_sq | (frm << 7) | ((1 << 14) if m.group(8) else 0)


def move16_to_usi(code: int) -> str:
    to_sq = code & 0x7F
    frm = (code >> 7) & 0x7F
    to_str = f"{to_sq // 9 + 1}{chr(ord('a') + to_sq % 9)}"
    if frm >= 81:  # drop
        return f"{'PLNSGBR'[frm - 81]}*{to_str}"
    frm_str = f"{frm // 9 + 1}{chr(ord('a') + frm % 9)}"
    promo = "+" if code & (1 << 14) else ""
    return f"{frm_str}{to_str}{promo}"


PIECE_KANJI = ["歩", "香", "桂", "銀", "金", "角", "飛", "玉",
               "と", "成香", "成桂", "成銀", "馬", "龍"]
_FILE_KANJI = "０１２３４５６７８９"
_RANK_KANJI = "〇一二三四五六七八九"


def move16_to_kanji(code: int, pos: Position | None = None) -> str:
    """'▲７六歩(77)' style notation. `pos` is the position before the move
    (used for the piece name and the mover mark); falls back to USI info."""
    to_sq = code & 0x7F
    frm = (code >> 7) & 0x7F
    dest = f"{_FILE_KANJI[to_sq // 9 + 1]}{_RANK_KANJI[to_sq % 9 + 1]}"
    mark = ""
    if pos is not None:
        mark = "▲" if pos.turn == BLACK else "△"
    if frm >= 81:
        return f"{mark}{dest}{PIECE_KANJI[frm - 81]}打"
    piece = ""
    if pos is not None and pos.board[frm] is not None:
        piece = PIECE_KANJI[pos.board[frm][1]]
    promo = "成" if code & (1 << 14) else ""
    return f"{mark}{dest}{piece}{promo}({frm // 9 + 1}{frm % 9 + 1})"


def apply_move16(pos: Position, code: int) -> None:
    """Apply a 16-bit move code to a position (no legality check)."""
    to_sq = code & 0x7F
    frm = (code >> 7) & 0x7F
    color = pos.turn
    if pos.board[to_sq] is not None:
        pos.hands[color][unpromote(pos.board[to_sq][1])] += 1
    if frm >= 81:
        piece = frm - 81
        pos.hands[color][piece] -= 1
        pos.board[to_sq] = (color, piece)
    else:
        moving = pos.board[frm]
        if moving is None:
            raise ValueError(f"no piece on square {frm}")
        t = moving[1]
        if code & (1 << 14):
            t = {KA: UM, HI: RY}.get(t, t + 8 if t < KI else t)
        pos.board[frm] = None
        pos.board[to_sq] = (color, t)
    pos.turn = color ^ 1


def format_pv_kanji(pos: Position, codes: list[int]) -> str:
    """Render a PV as Japanese notation, replaying on a scratch board."""
    scratch = pos.copy()
    parts = []
    for code in codes:
        parts.append(move16_to_kanji(code, scratch))
        try:
            apply_move16(scratch, code)
        except (ValueError, KeyError, TypeError):
            break
    return " ".join(parts)
