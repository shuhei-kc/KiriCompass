"""Encoding of per-ply engine analysis (eval + principal variation).

Storage layout (game_analysis table):
- evals: int16 little-endian array, one slot per position (ply 0..ply_count).
  Slot p holds the evaluation recorded between move p and move p+1,
  i.e. the search info for the position after p moves. Values are from
  Black's perspective (the convention used by floodgate, WCSC and
  Denryu-sen records; verified empirically against game results).
  NO_EVAL (-32768) marks slots without an evaluation.
- pvs: zlib-compressed framing: for each slot, one length byte followed by
  that many uint16 move codes (little endian). PVs longer than 255 moves
  are truncated. NULL when no PV was recorded in the whole game.
"""

from __future__ import annotations

import array
import zlib

NO_EVAL = -32768
EVAL_MAX = 32767


def clamp_eval(value: int) -> int:
    return max(-EVAL_MAX, min(EVAL_MAX, value))


def encode_evals(evals: list[int | None]) -> bytes:
    return array.array(
        "h", (NO_EVAL if v is None else clamp_eval(v) for v in evals)).tobytes()


def decode_evals(blob: bytes) -> list[int | None]:
    values = array.array("h")
    values.frombytes(blob)
    return [None if v == NO_EVAL else v for v in values]


def encode_pvs(pvs: list[list[int]]) -> bytes | None:
    if not any(pvs):
        return None
    out = bytearray()
    for pv in pvs:
        pv = pv[:255]
        out.append(len(pv))
        out += array.array("H", pv).tobytes()
    return zlib.compress(bytes(out), level=6)


def decode_pvs(blob: bytes | None) -> list[list[int]]:
    if blob is None:
        return []
    raw = zlib.decompress(blob)
    pvs, i = [], 0
    while i < len(raw):
        n = raw[i]
        i += 1
        moves = array.array("H")
        moves.frombytes(raw[i:i + 2 * n])
        i += 2 * n
        pvs.append(list(moves))
    return pvs
