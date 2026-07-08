"""KIF (Kakinoki) kifu parser.

Designed to be forgiving about real-world files:
- cp932 / UTF-8 (with or without BOM) / EUC-JP encodings
- full-width or half-width digits and colons
- 手合割 handicaps (駒落ち) and custom positions given as BOD diagrams
- 不成 annotations even where promotion is impossible (simply not promoting)
- piece-name mismatches between the move text and the board (the board wins,
  with a warning), 龍/竜 and 玉/王 spelling variants
- comment lines carrying engine analysis in the styles of ShogiHome,
  ShogiGUI, 棋神アナリティクス and K-Shogi/ぴよ将棋 (free-text comments are
  ignored); 変化 (variation) sections are skipped — only the main line is
  indexed as precedent data
"""

from __future__ import annotations

import re
from pathlib import Path

from .board import BLACK, WHITE, FU, KY, KE, GI, KI, KA, HI, OU, TO, NY, NK, NG, UM, RY, Position
from .csa import (DRAW_REASONS, GameRecord, CsaParseError, RESULT_BLACK,
                  RESULT_DRAW, RESULT_WHITE, parse_pv_tokens)

PIECE_FROM_KANJI = {
    "歩": FU, "香": KY, "桂": KE, "銀": GI, "金": KI, "角": KA, "飛": HI,
    "玉": OU, "王": OU, "と": TO, "成香": NY, "成桂": NK, "成銀": NG,
    "馬": UM, "龍": RY, "竜": RY,
}
PROMOTE = {FU: TO, KY: NY, KE: NK, GI: NG, KA: UM, HI: RY}

ZEN_DIGITS = "０１２３４５６７８９"
KAN_DIGITS = "〇一二三四五六七八九"

# 手合割 -> squares to remove from White (上手). sq = (file-1)*9 + (rank-1).
_HANDICAP_REMOVALS = {
    "平手": [],
    "香落ち": [(0, 0)],                       # 1一香
    "右香落ち": [(8, 0)],                     # 9一香
    "角落ち": [(1, 1)],                       # 2二角
    "飛車落ち": [(7, 1)],                     # 8二飛
    "飛香落ち": [(7, 1), (0, 0)],
    "二枚落ち": [(7, 1), (1, 1)],
    "三枚落ち": [(7, 1), (1, 1), (0, 0)],
    "四枚落ち": [(7, 1), (1, 1), (0, 0), (8, 0)],
    "五枚落ち": [(7, 1), (1, 1), (0, 0), (8, 0), (7, 0)],
    "左五枚落ち": [(7, 1), (1, 1), (0, 0), (8, 0), (1, 0)],
    "六枚落ち": [(7, 1), (1, 1), (0, 0), (8, 0), (7, 0), (1, 0)],
    "八枚落ち": [(7, 1), (1, 1), (0, 0), (8, 0), (7, 0), (1, 0), (6, 0), (2, 0)],
    "十枚落ち": [(7, 1), (1, 1), (0, 0), (8, 0), (7, 0), (1, 0), (6, 0), (2, 0),
              (5, 0), (3, 0)],
}

_TERMINAL_REASONS = {
    "投了": "toryo", "中断": "chudan", "千日手": "sennichite",
    "持将棋": "jishogi", "切れ負け": "time_up", "反則勝ち": "illegal_win",
    "反則負け": "illegal_move", "入玉勝ち": "kachi", "詰み": "tsumi",
    "不詰": "fuzumi", "引き分け": "hikiwake", "待った": "matta",
    "トライ勝ち": "try", "封じ手": None, "延期": None,  # None = not terminal
}

_MOVE_LINE_RE = re.compile(
    r"^\s*(\d+)\s+(.+?)(?:\s+\(\s*[0-9:／/ ]+\))?\s*$")
_FROM_RE = re.compile(r"\((\d)(\d)\)\s*$")
_FOOTER_RE = re.compile(
    r"^まで\d+手で(?:(先手|後手|下手|上手)の(反則勝ち|入玉勝ち|勝ち)"
    r"|(千日手|持将棋|中断|引き分け))")

# Engine-analysis comment styles found in KIF files (see ShogiHome
# common/record/comment.ts). All report scores from Black's perspective.
_KIF_EVAL_RES = [
    re.compile(r"^[*#]評価値=([+-]?\d+)"),                    # ShogiHome
    re.compile(r"^\*(?:対局|解析) .*?評価値 ([+-]?\d+)"),       # ShogiGUI
    re.compile(r"^\* .*?評価値 ([+-]?\d+)"),                  # 棋神アナリティクス
    re.compile(r"^#(?:形勢|指し手)\[([+-]?\d+)\]"),            # K-Shogi/ぴよ将棋
]
_KIF_PV_RES = [
    re.compile(r"^[*#]読み筋=(.+)$"),
    re.compile(r"^\*(?:対局|解析) .*?読み筋 (.+)$"),
]
# One KIF-style PV move: ▲７六歩(77) / △同 銀(31) / ▲２三歩成(24) / ▲５五角打
_KIF_PV_MOVE_RE = re.compile(
    r"[▲△▽▼☗☖]?(同|[１-９1-9][一二三四五六七八九1-9])[ 　]?"
    r"(成香|成桂|成銀|[歩香桂銀金角飛玉王と馬龍竜])(成|不成|打)?"
    r"(?:\((\d)(\d)\))?")


def _to_int(char: str) -> int:
    if char in ZEN_DIGITS:
        return ZEN_DIGITS.index(char)
    if char in KAN_DIGITS:
        return KAN_DIGITS.index(char)
    return int(char)


def decode_kif_bytes(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    for enc in ("utf-8", "cp932", "euc-jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


# BOD diagrams abbreviate promoted pieces to one character.
BOD_PIECE = dict(PIECE_FROM_KANJI)
BOD_PIECE.update({"杏": NY, "圭": NK, "全": NG})


def _parse_kanji_count(text: str) -> int:
    """'' -> 1, '二' -> 2, '十' -> 10, '十八' -> 18."""
    if not text:
        return 1
    total = 0
    for ch in text:
        if ch == "十":
            total = 10
        elif ch in KAN_DIGITS or ch in ZEN_DIGITS or ch.isdigit():
            total += _to_int(ch)
    return total or 1


def _parse_bod_hand(text: str) -> dict[int, int]:
    hand: dict[int, int] = {}
    text = text.replace("　", " ").replace("なし", "")
    for part in re.split(r"[ ,、]+", text.strip()):
        if not part:
            continue
        piece = BOD_PIECE.get(part[0])
        if piece is None:
            continue
        hand[piece] = hand.get(piece, 0) + _parse_kanji_count(part[1:])
    return hand


def parse_kif(text: str, source_name: str = "") -> GameRecord:
    rec = GameRecord()
    pos = Position()
    pos.set_hirate()

    lines = text.splitlines()
    headers: dict[str, str] = {}
    move_lines: list[tuple[int, str]] = []
    comments_by_slot: dict[int, list[str]] = {}
    bod_lines: list[str] = []
    footer_result: tuple | None = None
    terminal_word: str | None = None
    terminal_ply = 0
    in_variation = False
    current_slot = 0

    for line in lines:
        stripped = line.rstrip()
        if not stripped:
            continue
        if stripped.startswith("変化："):
            in_variation = True
            continue
        if in_variation:
            continue
        if stripped.startswith("*") or stripped.startswith("#"):
            comments_by_slot.setdefault(current_slot, []).append(stripped)
            continue
        m = _FOOTER_RE.match(stripped.replace("　", ""))
        if m:
            footer_result = m.groups()
            continue
        if stripped.startswith("|") or re.match(r"^\s*[９８７６５４３２１ 　]+$", stripped) \
                or stripped.startswith("+---"):
            bod_lines.append(stripped)
            continue
        if stripped.strip() in ("先手番", "後手番", "下手番", "上手番"):
            headers["手番"] = "先手" if stripped.strip() in ("先手番", "下手番") else "後手"
            continue
        m = _MOVE_LINE_RE.match(stripped)  # before header check: times contain ':'
        if m:
            number, body = int(m.group(1)), m.group(2).strip()
            word = body.split("(")[0].strip().replace("　", "")
            if word in _TERMINAL_REASONS:
                if _TERMINAL_REASONS[word] is not None:
                    terminal_word = word
                    terminal_ply = number
                continue
            move_lines.append((number, body))
            current_slot = len(move_lines)
            continue
        if "：" in stripped or ":" in stripped:
            sep = "：" if "：" in stripped else ":"
            key, _, value = stripped.partition(sep)
            key = key.strip()
            if key in ("先手の持駒", "後手の持駒", "上手の持駒", "下手の持駒"):
                bod_lines.append(stripped)
            else:
                headers.setdefault(key, value.strip())
            continue
        # tolerated noise (手数----指手 ruler, まで…, etc.)
        if not stripped.startswith(("手数", "まで")):
            rec.parse_warnings.append(f"unrecognized line: {stripped[:40]}")

    _apply_headers(rec, headers, source_name)

    handicap = headers.get("手合割", "平手").strip()
    first_turn = BLACK
    if bod_lines:
        _apply_bod(pos, bod_lines, headers)
        first_turn = WHITE if headers.get("手番") == "後手" else \
            (WHITE if handicap not in ("平手", "") else BLACK)
    elif handicap and handicap not in ("平手", ""):
        removals = _HANDICAP_REMOVALS.get(handicap)
        if removals is None:
            raise CsaParseError(f"unsupported 手合割: {handicap}")
        for file_idx, rank_idx in removals:
            pos.board[file_idx * 9 + rank_idx] = None
        first_turn = WHITE
    pos.turn = first_turn

    initial = pos.sfen_key_string()
    rec.initial_sfen = None if initial.endswith(" b -") and initial.startswith(
        "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL") else initial
    rec.sfen_keys.append(pos.position_key())

    # Replay main line.
    last_to: int | None = None
    parsed_comments = _prepare_analysis(rec, comments_by_slot, len(move_lines))
    if parsed_comments:
        _attach_kif_analysis(rec, pos, 0, parsed_comments)
    for number, body in move_lines:
        code, last_to = _apply_kif_move(pos, body, last_to, rec)
        rec.moves.append(code)
        rec.sfen_keys.append(pos.position_key())
        if parsed_comments:
            _attach_kif_analysis(rec, pos, len(rec.moves), parsed_comments)

    _finalize_kif_result(rec, terminal_word, terminal_ply, footer_result,
                         first_turn, len(rec.moves))
    return rec


def _apply_headers(rec: GameRecord, headers: dict, source_name: str) -> None:
    rec.black_name = headers.get("先手") or headers.get("下手") or ""
    rec.white_name = headers.get("後手") or headers.get("上手") or ""
    start = headers.get("開始日時") or headers.get("対局日") or ""
    m = re.match(r"(\d{4})[/年.-](\d{1,2})[/月.-](\d{1,2})日?"
                 r"(?:\s+(\d{1,2})[:：](\d{2})(?:[:：](\d{2}))?)?", start)
    if m:
        y, mo, d, h, mi, s = m.groups()
        rec.start_time = f"{y}-{int(mo):02d}-{int(d):02d} " \
                         f"{int(h or 0):02d}:{int(mi or 0):02d}:{int(s or 0):02d}"
    rec.event = Path(source_name).stem if source_name else \
        (headers.get("棋戦", "") + start).strip()


def _apply_bod(pos: Position, bod_lines: list[str], headers: dict) -> None:
    pos.board = [None] * 81
    pos.hands = ({t: 0 for t in range(7)}, {t: 0 for t in range(7)})
    rank_idx = 0
    for line in bod_lines:
        if line.startswith("|"):
            body = line[1:line.rfind("|")] if "|" in line[1:] else line[1:]
            # fixed cells: mark (space or 'v') + one piece char (or ・)
            file_idx = 8
            i = 0
            while i + 1 < len(body) and file_idx >= 0:
                mark, char = body[i], body[i + 1]
                if char != "・":
                    piece = BOD_PIECE.get(char)
                    if piece is not None:
                        color = WHITE if mark == "v" else BLACK
                        pos.board[file_idx * 9 + rank_idx] = (color, piece)
                i += 2
                file_idx -= 1
            rank_idx += 1
            continue
        for key, color in (("先手の持駒", BLACK), ("下手の持駒", BLACK),
                           ("後手の持駒", WHITE), ("上手の持駒", WHITE)):
            if line.startswith(key):
                hand = _parse_bod_hand(re.split("[：:]", line, 1)[-1])
                for piece, count in hand.items():
                    if piece < 7:
                        pos.hands[color][piece] += count


def _apply_kif_move(pos: Position, body: str, last_to: int | None,
                    rec: GameRecord) -> tuple[int, int]:
    text = body.replace("　", " ").strip()
    color = pos.turn

    m_from = _FROM_RE.search(text)
    frm = None
    if m_from:
        frm = (int(m_from.group(1)) - 1) * 9 + (int(m_from.group(2)) - 1)
        text = text[:m_from.start()].strip()

    if text.startswith("同"):
        if last_to is None:
            raise CsaParseError("同 with no previous move")
        to_sq = last_to
        text = text[1:].strip()
    else:
        if len(text) < 2:
            raise CsaParseError(f"unparsable move: {body}")
        to_sq = (_to_int(text[0]) - 1) * 9 + (_to_int(text[1]) - 1)
        text = text[2:].strip()

    promote = False
    drop = False
    if text.endswith("不成"):
        text = text[:-2]
    elif text.endswith("成") and text not in ("成香", "成桂", "成銀"):
        promote = True
        text = text[:-1]
    elif text.endswith("打"):
        drop = True
        text = text[:-1]
    piece = PIECE_FROM_KANJI.get(text.strip())
    if piece is None:
        raise CsaParseError(f"unknown piece in move: {body}")

    if drop or frm is None:
        if piece > HI:
            raise CsaParseError(f"cannot drop piece: {body}")
        return pos.push_csa(color, -1, to_sq, piece), to_sq

    on_board = pos.board[frm]
    if on_board is None or on_board[0] != color:
        raise CsaParseError(f"no piece to move for: {body}")
    if on_board[1] != piece:
        rec.parse_warnings.append(
            f"piece mismatch in '{body}' (board has {on_board[1]}); using board")
    base = on_board[1]
    after = PROMOTE.get(base, base) if promote else base
    return pos.push_csa(color, frm, to_sq, after), to_sq


def _prepare_analysis(rec: GameRecord, comments_by_slot: dict,
                      n_moves: int) -> dict | None:
    """Extract (eval, pv_text) per slot from raw KIF comment lines."""
    result: dict[int, tuple[int | None, str]] = {}
    for slot, lines in comments_by_slot.items():
        eval_value: int | None = None
        pv_text = ""
        for line in lines:
            for pattern in _KIF_EVAL_RES:
                m = pattern.match(line)
                if m:
                    eval_value = int(m.group(1))
                    break
            for pattern in _KIF_PV_RES:
                m = pattern.match(line)
                if m:
                    pv_text = m.group(1)
                    break
        if eval_value is not None or pv_text:
            result[slot] = (eval_value, pv_text)
    if not result:
        return None
    rec.evals = [None] * (n_moves + 1)
    rec.pvs = [[] for _ in range(n_moves + 1)]
    return result


def _attach_kif_analysis(rec: GameRecord, pos: Position, slot: int,
                         parsed: dict) -> None:
    entry = parsed.get(slot)
    if entry is None:
        return
    if entry[0] is not None:
        rec.evals[slot] = entry[0]
    if entry[1]:
        rec.pvs[slot] = _parse_kif_pv(entry[1], pos)


def _parse_kif_pv(pv_text: str, pos: Position) -> list[int]:
    """Parse a KIF-notation PV; stops at the first unresolvable move."""
    scratch = pos.copy()
    moves: list[int] = []
    last_to: int | None = None
    for m in _KIF_PV_MOVE_RE.finditer(pv_text):
        dest, piece_str, modifier, from_f, from_r = m.groups()
        if dest == "同":
            if last_to is None:
                break
            to_sq = last_to
        else:
            to_sq = (_to_int(dest[0]) - 1) * 9 + (_to_int(dest[1]) - 1)
        piece = PIECE_FROM_KANJI.get(piece_str)
        if piece is None:
            break
        color = scratch.turn
        try:
            if modifier == "打":
                code = scratch.push_csa(color, -1, to_sq, piece)
            elif from_f is not None:
                frm = (int(from_f) - 1) * 9 + (int(from_r) - 1)
                on_board = scratch.board[frm]
                if on_board is None:
                    break
                base = on_board[1]
                after = PROMOTE.get(base, base) if modifier == "成" else base
                code = scratch.push_csa(color, frm, to_sq, after)
            else:
                break  # no from-square and not a drop: cannot disambiguate
        except (ValueError, KeyError):
            break
        moves.append(code)
        last_to = to_sq
    return moves


def _finalize_kif_result(rec: GameRecord, terminal_word: str | None,
                         terminal_ply: int, footer, first_turn: int,
                         n_moves: int) -> None:
    reason = _TERMINAL_REASONS.get(terminal_word or "", "") or ""
    winner: int | None = None

    if footer is not None:
        side, kind, neutral = footer
        if neutral:
            reason = reason or {"千日手": "sennichite", "持将棋": "jishogi",
                                "中断": "chudan", "引き分け": "hikiwake"}[neutral]
            rec.end_reason = reason
            rec.finished = True
            rec.result = None if neutral == "中断" else RESULT_DRAW
            return
        winner = RESULT_BLACK if side in ("先手", "下手") else RESULT_WHITE
        if not reason:
            reason = {"反則勝ち": "illegal_move", "入玉勝ち": "kachi"}.get(kind, "toryo")

    if not reason and winner is None:
        rec.finished = False
        return

    rec.finished = True
    if reason == "illegal_win":
        # 反則勝ち recorded as a terminal move: the side to move wins.
        side_to_move = (first_turn + n_moves) % 2
        reason = "illegal_move"
        winner = RESULT_BLACK if side_to_move == BLACK else RESULT_WHITE
    rec.end_reason = reason
    if winner is not None:
        rec.result = winner
    elif reason in DRAW_REASONS:
        rec.result = RESULT_DRAW
    elif reason in ("chudan", "matta"):
        rec.result = None
    else:
        # terminal word without footer: 投了/切れ負け/詰み lose for side to move
        side_to_move = (first_turn + n_moves) % 2
        loser = side_to_move if reason in ("toryo", "time_up", "tsumi") else side_to_move ^ 1
        rec.result = RESULT_WHITE if loser == BLACK else RESULT_BLACK
