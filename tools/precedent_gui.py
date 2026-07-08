#!/usr/bin/env python3
"""前例ビューア (テストGUI): sfenを貼り付けて前例DBを検索する。

起動:  python3 tools/precedent_gui.py [db_path]

- sfen欄には "position sfen ...", "sfen ...", "startpos", 素のsfen の
  いずれを貼り付けてもよい。手数部分は無視される。
- 前例をダブルクリックすると棋譜URLをブラウザで開く。
- 前例を選択すると、その局面での評価値と読み筋(記録があれば)を表示する。
- 「ShogiHome連動」をONにすると、USIエンジン (tools/usi_engine.py) が
  書き出すsyncファイルを追従して自動検索する。単体利用時はOFFのまま。
"""

import json
import sys
import threading
import time
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kifudb.board import Position, normalize_sfen_main, usi_to_move16  # noqa: E402
from kifudb.ki2 import format_pv_ki2, move16_to_ki2  # noqa: E402
from kifudb.query import PrecedentReader, REASON_JA, format_report  # noqa: E402
from kifudb.usi import DEFAULT_SYNC_FILE, RUNTIME_DIR  # noqa: E402

SYNC_POLL_MS = 300

# 等幅かつ日本語対応のフォントをOSごとに優先順で探す。
MONO_FONT_CANDIDATES = [
    "BIZ UDゴシック", "BIZ UDGothic",          # Windows 10 1809+
    "ＭＳ ゴシック", "MS Gothic",
    "Osaka-等幅", "Osaka-Mono", "Osaka",       # macOS
    "Noto Sans Mono CJK JP", "IPAGothic",      # Linux
    "Menlo", "Consolas", "Courier New",
]


def setup_dpi_awareness() -> None:
    """Windowsで文字がぼやけないようにDPI対応を宣言する (Tk生成前に呼ぶ)。"""
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass


def pick_mono_font(root: tk.Tk) -> str:
    import tkinter.font as tkfont
    available = set(tkfont.families(root))
    for name in MONO_FONT_CANDIDATES:
        if name in available:
            return name
    return "TkFixedFont"

CONFIG_PATH = RUNTIME_DIR / "gui_config.json"
RESULT_JA = {1: "先手勝", 2: "後手勝", 0: "引分", None: "―"}
WINNER_JA = {1: "先", 2: "後"}  # それ以外 (引分・結果なし) は "-"


class PrecedentViewer:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("前例ビューア (kifudb)")
        root.geometry("1080x720")

        self.precedents = []
        self.query_position: Position | None = None
        self._sync_mtime: float | None = None
        self._search_running = False
        self._sync_file = DEFAULT_SYNC_FILE  # runtime/gui_config.json で上書き可
        self._reader: PrecedentReader | None = None

        self._setup_fonts()
        self._build_widgets()
        self._load_config()
        if len(sys.argv) > 1:
            self.db_var.set(sys.argv[1])
        self._poll_sync_file()

    def _setup_fonts(self) -> None:
        import tkinter.font as tkfont
        family = pick_mono_font(self.root)
        size = 13 if sys.platform == "darwin" else 11
        self.mono_font = tkfont.Font(family=family, size=size)
        heading_font = tkfont.nametofont("TkDefaultFont").copy()
        heading_font.configure(weight="bold")
        style = ttk.Style(self.root)
        row_height = self.mono_font.metrics("linespace") + 8
        style.configure("Treeview", font=self.mono_font, rowheight=row_height)
        style.configure("Treeview.Heading", font=heading_font)

    # -- layout --------------------------------------------------------

    def _build_widgets(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="DB:").grid(row=0, column=0, sticky=tk.W)
        self.db_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.db_var).grid(
            row=0, column=1, sticky=tk.EW, padx=4)
        ttk.Button(top, text="参照...", command=self._browse_db).grid(row=0, column=2)

        ttk.Label(top, text="SFEN:").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        self.sfen_var = tk.StringVar()
        sfen_entry = ttk.Entry(top, textvariable=self.sfen_var,
                               font=self.mono_font)
        sfen_entry.grid(row=1, column=1, sticky=tk.EW, padx=4, pady=(6, 0))
        sfen_entry.bind("<Return>", lambda _e: self.search())
        self.search_button = ttk.Button(top, text="検索", command=self.search)
        self.search_button.grid(row=1, column=2, pady=(6, 0))

        self.sync_var = tk.BooleanVar(value=False)
        sync_check = ttk.Checkbutton(
            top, text="ShogiHome連動 (USIエンジンの局面を追従)",
            variable=self.sync_var, command=self._on_sync_toggle)
        sync_check.grid(row=2, column=1, sticky=tk.W, padx=4, pady=(6, 0))
        top.columnconfigure(1, weight=1)

        panes = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        cand_frame = ttk.LabelFrame(panes, text="候補手")
        cand_cols = ("no", "move", "usi", "count", "black", "white", "draw", "rate")
        self.cand_tv = ttk.Treeview(cand_frame, columns=cand_cols,
                                    show="headings", height=6)
        for col, label, width, anchor in (
                ("no", "No.", 44, tk.E),
                ("move", "指し手", 140, tk.W), ("usi", "USI", 70, tk.W),
                ("count", "出現", 70, tk.E), ("black", "先手勝", 70, tk.E),
                ("white", "後手勝", 70, tk.E), ("draw", "引分", 60, tk.E),
                ("rate", "先手勝率", 80, tk.E)):
            self.cand_tv.heading(col, text=label)
            self.cand_tv.column(col, width=width, anchor=anchor, stretch=(col == "move"))
        self.cand_tv.pack(fill=tk.BOTH, expand=True)
        panes.add(cand_frame, weight=1)

        prec_frame = ttk.LabelFrame(panes, text="前例")
        prec_cols = ("no", "date", "black", "white", "next", "result", "reason",
                     "plies", "source")
        self.prec_tv = ttk.Treeview(prec_frame, columns=prec_cols, show="headings")
        for col, label, width, anchor in (
                ("no", "No.", 44, tk.E),
                ("date", "対局日", 110, tk.W), ("black", "先手", 170, tk.W),
                ("white", "後手", 170, tk.W), ("next", "次の一手", 96, tk.CENTER),
                ("result", "勝者", 44, tk.CENTER), ("reason", "終局理由", 68, tk.CENTER),
                ("plies", "手数", 50, tk.E), ("source", "出典", 80, tk.W)):
            self.prec_tv.heading(col, text=label)
            self.prec_tv.column(col, width=width, anchor=anchor,
                                stretch=col in ("black", "white"))
        scroll = ttk.Scrollbar(prec_frame, orient=tk.VERTICAL,
                               command=self.prec_tv.yview)
        self.prec_tv.configure(yscrollcommand=scroll.set)
        self.prec_tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.prec_tv.bind("<<TreeviewSelect>>", self._on_precedent_select)
        self.prec_tv.bind("<Double-1>", self._on_precedent_open)
        panes.add(prec_frame, weight=3)

        detail_frame = ttk.LabelFrame(panes, text="詳細 (評価値・読み筋・URL)")
        self.detail_text = tk.Text(detail_frame, height=5, wrap=tk.WORD,
                                   state=tk.DISABLED, font=self.mono_font)
        self.detail_text.pack(fill=tk.BOTH, expand=True)
        panes.add(detail_frame, weight=1)

        bottom = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        bottom.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="DBとsfenを指定して検索してください。")
        ttk.Label(bottom, textvariable=self.status_var).pack(side=tk.LEFT)
        ttk.Button(bottom, text="レポート保存...",
                   command=self._save_report).pack(side=tk.RIGHT)

    # -- actions ---------------------------------------------------------

    def _browse_db(self) -> None:
        path = filedialog.askopenfilename(
            title="前例DBを選択", filetypes=[("SQLite DB", "*.db *.sqlite"), ("All", "*")])
        if path:
            self.db_var.set(path)

    def _get_reader(self, db_path: str) -> PrecedentReader:
        """Persistent DB handle: keeps SQLite caches warm between searches."""
        if self._reader is not None and self._reader.db_path != db_path:
            self._reader.close()
            self._reader = None
        if self._reader is None:
            self._reader = PrecedentReader(db_path)
        return self._reader

    def search(self) -> None:
        if self._search_running:
            return
        db_path = self.db_var.get().strip()
        sfen = self.sfen_var.get().strip()
        if not db_path or not Path(db_path).is_file():
            messagebox.showerror("エラー", "有効なDBファイルを指定してください。")
            return
        if not sfen:
            messagebox.showerror("エラー", "sfenを貼り付けてください。")
            return
        self._save_config()
        self._search_running = True
        self.search_button.config(state=tk.DISABLED)
        self.status_var.set("検索中...")
        threading.Thread(target=self._search_task,
                         args=(db_path, sfen), daemon=True).start()

    def _search_task(self, db_path: str, sfen: str) -> None:
        try:
            sfen_main = normalize_sfen_main(sfen)
            position = Position()
            position.set_sfen(sfen_main)
            started = time.perf_counter()
            candidates, precedents, total = self._get_reader(db_path).lookup(sfen)
            elapsed = (time.perf_counter() - started) * 1000
            self.root.after(0, self._show_results, position, candidates,
                            precedents, total, elapsed)
        except Exception as exc:  # noqa: BLE001 - surface everything to the user
            self.root.after(0, self._show_error, str(exc))

    def _show_error(self, message: str) -> None:
        self._search_running = False
        self.search_button.config(state=tk.NORMAL)
        self.status_var.set("エラー")
        messagebox.showerror("検索エラー", message)

    def _show_results(self, position, candidates, precedents, total, elapsed) -> None:
        self._search_running = False
        self.search_button.config(state=tk.NORMAL)
        self.query_position = position
        self.precedents = precedents

        self.cand_tv.delete(*self.cand_tv.get_children())
        for rank, c in enumerate(candidates, start=1):
            code = usi_to_move16(c.usi)
            label = (move16_to_ki2(position, code) if code is not None
                     else "(終局)" if c.usi == "(end)" else c.usi)
            decided = c.black_wins + c.white_wins
            rate = f"{c.black_wins / decided * 100:.1f}%" if decided else "-"
            self.cand_tv.insert("", tk.END, values=(
                rank, label, c.usi, c.game_count, c.black_wins, c.white_wins,
                c.draws, rate))

        self.prec_tv.delete(*self.prec_tv.get_children())
        for index, p in enumerate(precedents):
            code = usi_to_move16(p.next_move_usi) if p.next_move_usi else None
            next_label = (move16_to_ki2(position, code) if code is not None
                          else "(終局)")
            self.prec_tv.insert("", tk.END, iid=str(index), values=(
                index + 1, p.started_at[:10].replace("-", "/"),
                p.black_name, p.white_name, next_label,
                WINNER_JA.get(p.result, "-"),
                REASON_JA.get(p.end_reason, p.end_reason),
                p.ply_count, p.source))

        self._set_detail("前例を選択すると評価値・読み筋・URLを表示します。")
        self.status_var.set(
            f"前例 {total}局 / 候補手 {len(candidates)}種 / 表示 {len(precedents)}件 "
            f"({elapsed:.1f}ms)")

    def _on_precedent_select(self, _event=None) -> None:
        p = self._selected_precedent()
        if p is None:
            return
        lines = [f"{p.black_name} vs {p.white_name}  "
                 f"{p.started_at[:10].replace('-', '/')}  "
                 f"{RESULT_JA.get(p.result, '?')} ({REASON_JA.get(p.end_reason, p.end_reason)}) "
                 f"{p.ply_count}手"]
        detail = self._get_reader(self.db_var.get().strip()).get_game(p.game_id)
        if detail and detail.evals:
            ply = p.ply
            eval_here = detail.evals[ply] if ply < len(detail.evals) else None
            lines.append(f"この局面の評価値: {eval_here if eval_here is not None else '記録なし'}"
                         " (先手視点)")
            if ply < len(detail.pvs_usi) and detail.pvs_usi[ply] and self.query_position:
                codes = [usi_to_move16(u) for u in detail.pvs_usi[ply]]
                codes = [c for c in codes if c is not None]
                lines.append("読み筋: " + format_pv_ki2(self.query_position, codes))
        else:
            lines.append("評価値・読み筋の記録なし")
        if p.url:
            lines.append(f"URL: {p.url} (ダブルクリックで開く)")
        self._set_detail("\n".join(lines))

    def _on_precedent_open(self, _event=None) -> None:
        p = self._selected_precedent()
        if p is not None and p.url:
            webbrowser.open(p.url)

    def _selected_precedent(self):
        selection = self.prec_tv.selection()
        if not selection or not self.precedents:
            return None
        try:
            return self.precedents[int(selection[0])]
        except (ValueError, IndexError):
            return None

    def _set_detail(self, text: str) -> None:
        self.detail_text.config(state=tk.NORMAL)
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert("1.0", text)
        self.detail_text.config(state=tk.DISABLED)

    def _save_report(self) -> None:
        if self.query_position is None:
            messagebox.showinfo("情報", "先に検索してください。")
            return
        path = filedialog.asksaveasfilename(
            title="レポート保存", defaultextension=".txt",
            filetypes=[("Text", "*.txt")])
        if not path:
            return
        candidates, precedents, total = self._get_reader(
            self.db_var.get().strip()).lookup(self.sfen_var.get().strip())
        report = format_report(self.sfen_var.get().strip(), candidates,
                               precedents, total)
        Path(path).write_text(report, encoding="utf-8")
        self.status_var.set(f"保存しました: {path}")

    # -- ShogiHome sync ----------------------------------------------------

    def _on_sync_toggle(self) -> None:
        if self.sync_var.get():
            self._sync_mtime = None  # pick up the current position immediately
            self.status_var.set(f"連動待機中: {self._sync_file}")
        else:
            self.status_var.set("連動を解除しました。")

    def _poll_sync_file(self) -> None:
        try:
            if self.sync_var.get():
                self._check_sync_file()
        finally:
            self.root.after(SYNC_POLL_MS, self._poll_sync_file)

    def _check_sync_file(self) -> None:
        try:
            mtime = self._sync_file.stat().st_mtime
        except OSError:
            return
        if mtime == self._sync_mtime or self._search_running:
            return
        try:
            data = json.loads(self._sync_file.read_text(encoding="utf-8"))
            sfen = data["sfen"]
        except (OSError, json.JSONDecodeError, KeyError):
            return
        self._sync_mtime = mtime
        if sfen and sfen != self.sfen_var.get().strip():
            self.sfen_var.set(sfen)
            if self.db_var.get().strip():
                self.search()

    # -- config ----------------------------------------------------------

    def _load_config(self) -> None:
        try:
            config = json.loads(CONFIG_PATH.read_text())
            self.db_var.set(config.get("db_path", ""))
            self.sfen_var.set(config.get("last_sfen", ""))
            if config.get("sync_file"):
                self._sync_file = Path(config["sync_file"])
        except (OSError, json.JSONDecodeError):
            pass

    def _save_config(self) -> None:
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps({
                "db_path": self.db_var.get().strip(),
                "last_sfen": self.sfen_var.get().strip()}, ensure_ascii=False))
        except OSError:
            pass


def main() -> None:
    setup_dpi_awareness()
    root = tk.Tk()
    PrecedentViewer(root)
    root.mainloop()


if __name__ == "__main__":
    main()
