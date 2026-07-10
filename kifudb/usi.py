"""USI engine that answers from the precedent database.

Register the launcher script in ShogiHome (or any USI GUI) and use it in
research (検討) mode: every position you navigate to is looked up in the
database and the top precedent moves are reported as multipv info lines
(move counts appear in the "nodes" column, scores are win rates mapped to
centipawns from the side to move's perspective). Each PV is a greedy walk
along the most frequent precedent continuation.

The engine also mirrors every `position` command into a small JSON sync
file, which the precedent viewer GUI can follow (one-directional link,
both programs stay independently usable).

Behaviour without precedents: `info string 前例なし` and, when a bestmove
is required, `bestmove resign`. For a book-only engine resigning is the
honest signal that its knowledge is exhausted; in research mode ShogiHome
effectively ignores bestmove, so nothing breaks either way.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .board import Position, apply_move16, normalize_sfen_main, usi_to_move16
from .query import PrecedentReader

# Runtime files (sync file, launcher, GUI config) live inside the package
# tree so nothing is scattered around the user's home directory.
RUNTIME_DIR = Path(__file__).resolve().parent.parent / "runtime"
DEFAULT_SYNC_FILE = RUNTIME_DIR / "sync_position.json"

ENGINE_NAME = "KifuDB Precedents"
ENGINE_AUTHOR = "478"


class PrecedentUsiEngine:
    def __init__(self, db_path: str = "", sync_file: str | Path = DEFAULT_SYNC_FILE,
                 multipv: int = 8, pv_depth: int = 12,
                 reader=None, writer=None, log_file: str | None = None) -> None:
        self.db_path = db_path
        self.sync_file = Path(sync_file) if sync_file else None
        self.multipv = multipv
        self.pv_depth = pv_depth
        self.reader = reader or sys.stdin
        self.writer = writer or sys.stdout
        self.log_path = log_file

        self.position = Position()
        self.position.set_hirate()
        self.ply = 0
        self.pending_bestmove: str | None = None
        self._reader: PrecedentReader | None = None

    def _get_reader(self) -> PrecedentReader | None:
        """Persistent DB handle (kept open so lookups stay warm)."""
        if self._reader is not None and self._reader.db_path != self.db_path:
            self._reader.close()
            self._reader = None
        if self._reader is None and self.db_path and Path(self.db_path).is_file():
            self._reader = PrecedentReader(self.db_path)
        return self._reader

    # -- plumbing ----------------------------------------------------------

    def send(self, line: str) -> None:
        self.writer.write(line + "\n")
        self.writer.flush()

    def log(self, message: str) -> None:
        if self.log_path:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} {message}\n")
            except OSError:
                pass

    # -- main loop ----------------------------------------------------------

    def run(self) -> None:
        for raw in self.reader:
            line = raw.strip()
            if not line:
                continue
            self.log(f"< {line}")
            try:
                if not self.dispatch(line):
                    break
            except Exception as exc:  # noqa: BLE001 - a USI engine must not die
                self.log(f"ERROR {exc!r}")
                self.send(f"info string error: {exc}")

    def dispatch(self, line: str) -> bool:
        parts = line.split()
        command = parts[0]
        if command == "usi":
            self.send(f"id name {ENGINE_NAME}")
            self.send(f"id author {ENGINE_AUTHOR}")
            self.send(f"option name DbPath type string default {self.db_path or '<unset>'}")
            self.send(f"option name SyncFile type string default {self.sync_file or ''}")
            self.send(f"option name MultiPV type spin default {self.multipv} min 1 max 50")
            self.send(f"option name PvDepth type spin default {self.pv_depth} min 1 max 100")
            self.send(f"option name OutputEncoding type combo "
                      f"default {self._output_encoding()} var utf-8 var cp932")
            self.send("usiok")
        elif command == "setoption":
            self._setoption(parts)
        elif command == "isready":
            if self.db_path and Path(self.db_path).is_file():
                self.send("readyok")
            else:
                self.send(f"info string DB not found: {self.db_path!r} "
                          "(set DbPath or pass --db)")
                self.send("readyok")  # stay registered; lookups will report no data
        elif command == "usinewgame":
            pass
        elif command == "position":
            self._set_position(parts[1:])
        elif command == "go":
            self._go(parts[1:])
        elif command == "stop":
            self._flush_bestmove()
        elif command == "ponderhit":
            pass
        elif command == "gameover":
            self.pending_bestmove = None
        elif command == "quit":
            return False
        return True

    # -- commands -----------------------------------------------------------

    def _setoption(self, parts: list[str]) -> None:
        try:
            name = parts[parts.index("name") + 1]
            value = " ".join(parts[parts.index("value") + 1:])
        except (ValueError, IndexError):
            return
        key = name.lower()
        if key == "dbpath":
            self.db_path = value
        elif key == "syncfile":
            self.sync_file = Path(value) if value else None
        elif key == "multipv":
            self.multipv = max(1, int(value))
        elif key == "pvdepth":
            self.pv_depth = max(1, int(value))
        elif key == "outputencoding":
            self.set_output_encoding(value)

    def _output_encoding(self) -> str:
        current = (getattr(self.writer, "encoding", None) or "utf-8").lower()
        return "cp932" if current in ("cp932", "shift_jis", "mbcs") else "utf-8"

    def set_output_encoding(self, encoding: str) -> None:
        """info文字列の出力エンコーディングを切り替える。

        ShogiHome はエンジン出力をUTF-8で読むが、ShogiGUI・将棋所 (日本語
        Windows) はCP932を期待するため、日本語のinfo文字列が化ける。GUIの
        エンジン設定 (setoption OutputEncoding) か --encoding で合わせる。"""
        if encoding not in ("utf-8", "cp932"):
            return
        try:
            self.writer.reconfigure(encoding=encoding, errors="replace")
        except (AttributeError, OSError, LookupError):
            pass

    def _set_position(self, args: list[str]) -> None:
        moves: list[str] = []
        if "moves" in args:
            index = args.index("moves")
            moves = args[index + 1:]
            args = args[:index]

        self.position = Position()
        self.ply = 0
        if not args or args[0] == "startpos":
            self.position.set_hirate()
        elif args[0] == "sfen":
            sfen = " ".join(args[1:])
            self.position.set_sfen(normalize_sfen_main(sfen))
            tail = args[-1]
            if tail.isdigit():
                self.ply = int(tail) - 1
        for usi in moves:
            code = usi_to_move16(usi)
            if code is None:
                self.send(f"info string unparsable move ignored: {usi}")
                break
            try:
                apply_move16(self.position, code)
            except (ValueError, KeyError, TypeError):
                self.send(f"info string illegal move ignored: {usi}")
                break
            self.ply += 1
        self._write_sync()

    def _go(self, args: list[str]) -> None:
        bestmove = self._report_precedents()
        if "infinite" in args or "ponder" in args:
            self.pending_bestmove = bestmove  # sent on 'stop'
        else:
            self.pending_bestmove = None
            self.send(f"bestmove {bestmove}")

    def _flush_bestmove(self) -> None:
        if self.pending_bestmove is not None:
            self.send(f"bestmove {self.pending_bestmove}")
            self.pending_bestmove = None

    # -- precedent reporting -------------------------------------------------

    def _report_precedents(self) -> str:
        """Emit info lines for the current position; return the bestmove."""
        reader = self._get_reader()
        if reader is None:
            self.send("info string no database configured")
            return "resign"

        sfen = self.position.sfen_key_string()
        started = time.perf_counter()
        candidates, _, total = reader.lookup(sfen, max_precedents=1)
        moves = [c for c in candidates if c.usi != "(end)"]

        if total == 0:
            self.send("info depth 0 score cp 0 string 前例なし")
            return "resign"

        elapsed_ms = max(1, round((time.perf_counter() - started) * 1000))
        self.send(f"info string 前例 {total}局 / 候補 {len(moves)}手")

        # Scores are deliberately reported as 0: win-rate-to-centipawn
        # conversions look authoritative but carry little meaning here.
        # The information lives in the nodes column (= game count) and PV.
        for rank, cand in enumerate(moves[:self.multipv], start=1):
            pv = self._walk_pv(reader, cand.usi)
            self.send(f"info multipv {rank} depth {len(pv)} seldepth {len(pv)} "
                      f"time {elapsed_ms} nodes {cand.game_count} "
                      f"score cp 0 pv {' '.join(pv)}")

        if not moves:
            # every precedent game ended at this position
            self.send("info string 前例はこの局面で終局しています")
            return "resign"
        return moves[0].usi

    def _walk_pv(self, reader: PrecedentReader, first_usi: str) -> list[str]:
        """Greedy line following the most frequent precedent continuation."""
        pv = [first_usi]
        scratch = self.position.copy()
        code = usi_to_move16(first_usi)
        if code is None:
            return pv
        try:
            apply_move16(scratch, code)
        except (ValueError, KeyError, TypeError):
            return pv
        for _ in range(self.pv_depth - 1):
            candidates, _, total = reader.lookup(scratch.sfen_key_string(),
                                                 max_precedents=0)
            moves = [c for c in candidates if c.usi != "(end)"]
            if not moves or total == 0:
                break
            best = moves[0].usi
            code = usi_to_move16(best)
            if code is None:
                break
            try:
                apply_move16(scratch, code)
            except (ValueError, KeyError, TypeError):
                break
            pv.append(best)
        return pv

    # -- sync ----------------------------------------------------------------

    def _write_sync(self) -> None:
        if self.sync_file is None:
            return
        try:
            self.sync_file.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({
                "sfen": self.position.sfen_key_string(),
                "ply": self.ply,
                "updated": time.time(),
            }, ensure_ascii=False)
            tmp = self.sync_file.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.sync_file)
        except OSError as exc:
            self.log(f"sync write failed: {exc!r}")
