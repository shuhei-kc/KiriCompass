"""CSA kifu parser covering floodgate (V2), WCSC (V2.2) and Denryu-sen files.

Produces normalized GameRecord objects. Handles:
- V2/V2.2 headers, CRLF, cp932/utf-8/euc-jp encodings
- PI (hirate), PI with removed pieces (handicap), explicit P1..P9 boards,
  P+/P- hand pieces including AL
- comma-separated statements on one line
- '%' special moves, floodgate "'summary:" lines
- unfinished games (reported, never silently ingested)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .board import BLACK, WHITE, CSA_PIECE, FULL_SET, Position, unpromote

# Normalized end reason tokens (stored in DB, mapped to Japanese in the UI).
SPECIAL_TO_REASON = {
    "TORYO": "toryo",
    "TIME_UP": "time_up",
    "ILLEGAL_MOVE": "illegal_move",
    "ILLEGAL_ACTION": "illegal_action",
    "SENNICHITE": "sennichite",
    "OUTE_SENNICHITE": "oute_sennichite",
    "JISHOGI": "jishogi",
    "KACHI": "kachi",
    "HIKIWAKE": "hikiwake",
    "MAX_MOVES": "max_moves",
    "MATTA": "matta",
    "CHUDAN": "chudan",
    "UCHIFUZUME": "uchifuzume",
    "OUTE_KAIHIMORE": "oute_kaihimore",
    "TSUMI": "tsumi",
    "FUZUMI": "fuzumi",
    "ERROR": "error",
    "ABNORMAL": "abnormal",
}
# Reasons that leave the game with no winner.
DRAW_REASONS = {"sennichite", "jishogi", "hikiwake", "max_moves"}
NO_RESULT_REASONS = {"chudan", "matta", "error", "abnormal"}

RESULT_DRAW, RESULT_BLACK, RESULT_WHITE = 0, 1, 2

_SUMMARY_RE = re.compile(
    r"^summary:(?P<reason>[^:]+):(?P<p1>.+?)\s+(?P<r1>win|lose|draw)"
    r":(?P<p2>.+?)\s+(?P<r2>win|lose|draw)\s*$", re.I)
_MOVE_RE = re.compile(r"^([+-])(\d\d)(\d\d)([A-Z]{2})$")
# Engine analysis comment: '** <eval> [pv...] (floodgate/WCSC/Denryu-sen;
# also accepts single-star variants). Free-text comments never match.
_EVAL_COMMENT_RE = re.compile(r"^'\*{1,2}\s*([+-]?\d+)(?:\s+(.+))?$")
_CSA_PV_TOKEN_RE = re.compile(r"^([+-])(\d\d)(\d\d)([A-Z]{2})$")


@dataclass
class GameRecord:
    black_name: str = ""
    white_name: str = ""
    event: str = ""
    start_time: str = ""          # "YYYY-MM-DD HH:MM:SS" when known
    initial_sfen: str | None = None  # None = hirate
    moves: list[int] = field(default_factory=list)      # move16 codes
    sfen_keys: list[int] = field(default_factory=list)  # position key per ply, len = moves+1
    result: int | None = None     # RESULT_* or None
    end_reason: str = ""          # normalized token, "" = unfinished
    finished: bool = False
    parse_warnings: list[str] = field(default_factory=list)
    # Engine analysis, one slot per position (len = moves+1 when present).
    # evals[p] / pvs[p] describe the position after p moves, from Black's
    # perspective. Empty lists = no analysis found in the record.
    evals: list[int | None] = field(default_factory=list)
    pvs: list[list[int]] = field(default_factory=list)

    @property
    def has_analysis(self) -> bool:
        return any(v is not None for v in self.evals) or any(self.pvs)


class CsaParseError(Exception):
    pass


def decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "cp932", "euc-jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _statements(text: str):
    """Yield individual CSA statements.

    Handles two real-world quirks besides plain lines:
    - comma-joined statements ("+2726FU,T0")
    - inline comments on move/time lines ("+2726FU '** 0 +2726FU", seen in
      Marin/dbga records). The comment describes the position *before* the
      move on the same line (its PV starts with that move), so it is yielded
      first to land in the correct analysis slot.
    """
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("'"):
            yield line  # comments may contain commas (summary line)
            continue
        if line[0] in "+-%T":
            quote = line.find("'")
            if quote > 0:
                yield line[quote:].rstrip()
                line = line[:quote].rstrip()
                if not line:
                    continue
        for part in line.split(","):
            part = part.strip()
            if part:
                yield part


def _sq(two_digits: str) -> int:
    file_n, rank_n = int(two_digits[0]), int(two_digits[1])
    return (file_n - 1) * 9 + (rank_n - 1)


def _normalize_time(value: str) -> str:
    value = value.strip().replace("/", "-")
    return value


def parse_csa(text: str, source_name: str = "") -> GameRecord:
    """Parse one CSA game and replay it to collect position keys."""
    rec = GameRecord()
    pos = Position()

    board_rows_seen = False
    setup_done = False
    removed_by_pi: list[tuple[int, int]] = []
    hand_lines: list[str] = []
    explicit_board: list[str] = []
    use_pi = False
    first_turn = BLACK
    summary_reason: str | None = None
    summary_result: int | None = None
    special: str | None = None
    special_sign = ""
    csa_moves: list[tuple[int, int, int, int]] = []

    eval_comments: dict[int, tuple[int, str]] = {}

    for st in _statements(text):
        if st.startswith("'"):
            m_eval = _EVAL_COMMENT_RE.match(st)
            if m_eval:
                # Slot = number of moves seen so far: the comment describes
                # the current position and precedes the next move line.
                eval_comments[len(csa_moves)] = (int(m_eval.group(1)),
                                                 m_eval.group(2) or "")
                continue
            body = st[1:].strip()
            m = _SUMMARY_RE.match(body)
            if m:
                summary_reason = m.group("reason").strip().lower().replace(" ", "_")
                p1, r1 = m.group("p1").strip(), m.group("r1").lower()
                r2 = m.group("r2").lower()
                if r1 == "draw" and r2 == "draw":
                    summary_result = RESULT_DRAW
                elif r1 == "win":
                    summary_result = RESULT_BLACK if p1 == rec.black_name else RESULT_WHITE
                elif r2 == "win":
                    summary_result = RESULT_WHITE if p1 == rec.black_name else RESULT_BLACK
            elif body.startswith("$END_TIME:") or body.startswith("$START_TIME:"):
                pass  # informational
            continue
        if st.startswith("V"):
            continue
        if st.startswith("N+"):
            rec.black_name = st[2:].strip()
            continue
        if st.startswith("N-"):
            rec.white_name = st[2:].strip()
            continue
        if st.startswith("$EVENT:"):
            rec.event = st[7:].strip()
            continue
        if st.startswith("$START_TIME:"):
            rec.start_time = _normalize_time(st[12:])
            continue
        if st.startswith("$"):
            continue
        if st.startswith("PI"):
            use_pi = True
            body = st[2:]
            for i in range(0, len(body) - 3, 4):
                removed_by_pi.append((_sq(body[i:i + 2]), CSA_PIECE[body[i + 2:i + 4]]))
            continue
        if st.startswith(("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9")):
            explicit_board.append(st)
            board_rows_seen = True
            continue
        if st.startswith(("P+", "P-")):
            hand_lines.append(st)
            continue
        if st in ("+", "-") and not setup_done:
            first_turn = BLACK if st == "+" else WHITE
            _apply_setup(pos, use_pi, removed_by_pi, explicit_board, hand_lines,
                         board_rows_seen, first_turn)
            setup_done = True
            rec.sfen_keys.append(pos.position_key())
            initial = pos.sfen_key_string()
            rec.initial_sfen = None if initial.endswith(" b -") and initial.startswith(
                "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL") else initial
            continue
        if st.startswith("%"):
            token = st[1:].strip().upper().replace(" ", "_")
            if token and token[0] in "+-":
                special_sign = token[0]
                token = token[1:]
            if special is None:
                special = token
            # break しない: 終局トークンの後にも 'summary: 行が来る
            # (floodgate等)。勝敗はパリティ推定よりsummaryが正確。
            continue
        if st.startswith("T"):
            continue
        m = _MOVE_RE.match(st)
        if m:
            if special is not None:
                continue  # 終局後の指し手行は無視
            if not setup_done:
                raise CsaParseError("move before position setup / turn line")
            color = BLACK if m.group(1) == "+" else WHITE
            frm = -1 if m.group(2) == "00" else _sq(m.group(2))
            csa_moves.append((color, frm, _sq(m.group(3)), CSA_PIECE[m.group(4)]))
            continue
        rec.parse_warnings.append(f"unrecognized statement: {st[:40]}")

    if not setup_done:
        raise CsaParseError("no position setup found")

    # Replay, converting analysis comments at each position.
    has_analysis = bool(eval_comments)
    if has_analysis:
        rec.evals = [None] * (len(csa_moves) + 1)
        rec.pvs = [[] for _ in range(len(csa_moves) + 1)]
        _attach_analysis(rec, pos, 0, eval_comments)
    prev_pos = None
    for color, frm, to, ptype in csa_moves:
        if has_analysis:
            prev_pos = pos.copy()
        try:
            rec.moves.append(pos.push_csa(color, frm, to, ptype))
        except (ValueError, KeyError) as exc:
            raise CsaParseError(f"replay failed at ply {len(rec.moves) + 1}: {exc}") from exc
        rec.sfen_keys.append(pos.position_key())
        if has_analysis:
            slot = len(rec.moves)
            entry = eval_comments.get(slot)
            if entry is not None and _pv_starts_with(entry[1], rec.moves[-1]):
                # 一部のエンジンは「自分の指し手の直後」に、その手を指す
                # 前の局面の解析を書く (PVが直前の自手から始まるのが特徴。
                # 直前に指された手をもう一度指すことは物理的に不可能なので
                # 誤検出はない)。1スロット前の局面に付け替える。
                if rec.evals[slot - 1] is None and not rec.pvs[slot - 1]:
                    rec.evals[slot - 1] = entry[0]
                    if entry[1]:
                        rec.pvs[slot - 1] = parse_pv_tokens(entry[1], prev_pos)
                continue
            _attach_analysis(rec, pos, slot, eval_comments)

    _finalize_result(rec, special, special_sign, summary_reason, summary_result,
                     len(csa_moves), first_turn)
    if not rec.start_time and source_name:
        m = re.search(r"(\d{14})", Path(source_name).stem[::-1])
        if m:
            ts = m.group(1)[::-1]
            rec.start_time = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}"
    if not rec.event and source_name:
        rec.event = Path(source_name).stem
    return rec


def _pv_starts_with(pv_text: str, move_code: int) -> bool:
    """True if the PV's first token denotes exactly `move_code`."""
    from .board import usi_to_move16

    tokens = pv_text.split()
    if not tokens:
        return False
    token = tokens[0]
    m = _CSA_PV_TOKEN_RE.match(token)
    if m:
        to_sq = _sq(m.group(3))
        if m.group(2) == "00":
            frm_field = 81 + CSA_PIECE.get(m.group(4), 99)
        else:
            frm_field = _sq(m.group(2))
        return (move_code & 0x3FFF) == (to_sq | (frm_field << 7))
    code = usi_to_move16(token)
    return code is not None and (code & 0x3FFF) == (move_code & 0x3FFF)


def _attach_analysis(rec: GameRecord, pos, slot: int, eval_comments: dict) -> None:
    entry = eval_comments.get(slot)
    if entry is None:
        return
    rec.evals[slot] = entry[0]
    if entry[1]:
        rec.pvs[slot] = parse_pv_tokens(entry[1], pos)


def parse_pv_tokens(pv_text: str, pos) -> list[int]:
    """Convert a PV string (CSA or USI move tokens) to move16 codes.

    Tolerant by design: parsing stops at the first token that is neither a
    CSA nor a USI move (times, node counts, '%TORYO', free text, ...).
    Both CSA and USI tokens are replayed on a scratch board; tails that do
    not apply to the position are truncated. This keeps the stored PV
    consistent with the position, so a kifu reconstructed from the database
    reproduces exactly the same analysis data.
    """
    from .board import apply_move16, usi_to_move16

    tokens = pv_text.split()
    moves: list[int] = []
    scratch = pos.copy()
    for token in tokens:
        m = _CSA_PV_TOKEN_RE.match(token)
        if m:
            color = BLACK if m.group(1) == "+" else WHITE
            frm = -1 if m.group(2) == "00" else _sq(m.group(2))
            try:
                moves.append(scratch.push_csa(color, frm, _sq(m.group(3)),
                                              CSA_PIECE[m.group(4)]))
            except (ValueError, KeyError):
                break
            continue
        code = usi_to_move16(token)
        if code is not None:
            try:
                apply_move16(scratch, code)
            except (ValueError, KeyError, TypeError):
                break
            moves.append(code)
            continue
        break
    return moves


def _apply_setup(pos, use_pi, removed, explicit_board, hand_lines,
                 board_rows_seen, first_turn) -> None:
    if board_rows_seen:
        pos.board = [None] * 81
        for st in explicit_board:
            rank = int(st[1]) - 1
            body = st[2:]
            for file_idx in range(9):
                cell = body[file_idx * 3:file_idx * 3 + 3]
                if len(cell) < 3 or cell[0] == " ":
                    continue
                color = BLACK if cell[0] == "+" else WHITE
                pos.board[(8 - file_idx) * 9 + rank] = (color, CSA_PIECE[cell[1:3]])
    else:
        pos.set_hirate()
        if use_pi:
            for sq, ptype in removed:
                piece = pos.board[sq]
                if piece is not None and piece[1] == ptype:
                    pos.board[sq] = None
    for st in hand_lines:
        color = BLACK if st[1] == "+" else WHITE
        body = st[2:]
        for i in range(0, len(body) - 3, 4):
            code = body[i + 2:i + 4]
            if code == "AL":
                remaining = dict(FULL_SET)
                for piece in pos.board:
                    if piece is not None:
                        remaining[unpromote(piece[1])] -= 1
                for c in (BLACK, WHITE):
                    for t, n in pos.hands[c].items():
                        remaining[t] -= n
                for t, n in remaining.items():
                    if t != 7 and n > 0:  # OU never goes to hand
                        pos.hands[color][t] += n
            else:
                pos.hands[color][CSA_PIECE[code]] += 1
    pos.turn = first_turn


def _finalize_result(rec, special, sign, summary_reason, summary_result,
                     n_moves, first_turn) -> None:
    side_to_move = (first_turn + n_moves) % 2

    reason = ""
    if special is not None:
        reason = SPECIAL_TO_REASON.get(special, special.lower())
    elif summary_reason:
        reason = summary_reason
    rec.end_reason = reason
    rec.finished = bool(reason)
    if not rec.finished:
        return

    if summary_result is not None:
        rec.result = summary_result
        return
    if reason in DRAW_REASONS:
        rec.result = RESULT_DRAW
    elif reason in NO_RESULT_REASONS:
        rec.result = None
    elif reason == "kachi":
        rec.result = RESULT_BLACK if side_to_move == BLACK else RESULT_WHITE
    elif reason in ("illegal_action",) and sign:
        rec.result = RESULT_WHITE if sign == "+" else RESULT_BLACK
    elif reason in ("toryo", "time_up", "tsumi", "illegal_move",
                    "uchifuzume", "oute_kaihimore", "oute_sennichite"):
        # The side to move (or the offender who just moved illegally) loses.
        loser = side_to_move if reason in ("toryo", "time_up", "tsumi") else side_to_move ^ 1
        rec.result = RESULT_WHITE if loser == BLACK else RESULT_BLACK
    else:
        rec.result = None
