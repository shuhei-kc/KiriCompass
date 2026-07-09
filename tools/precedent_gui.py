#!/usr/bin/env python3
"""前例ビューア (テストGUI): sfenを貼り付けて前例DBを検索する。

起動:  python3 tools/precedent_gui.py [db_path]

- sfen欄には "position sfen ...", "sfen ...", "startpos", 素のsfen の
  いずれを貼り付けてもよい。手数部分は無視される。
- 前例をダブルクリックすると棋譜ビューアをブラウザで開く (URLが無い前例は
  棋譜のクリップボードコピーにフォールバック)。
  「棋譜コピー」ボタンは棋譜をクリップボードにコピーする。ShogiHomeの検討中
  ウィンドウに Ctrl+V (⌘V) でそのまま貼り付けられる (元ファイルが残っていれば
  その内容を、なければDBから復元した棋譜を使う)。
  「ファイルで開く」ボタンは既定アプリで開く (新規ウィンドウ)。
- 前例を選択すると、その局面での評価値と読み筋(記録があれば)を表示する。
- 「ShogiHome連動」をONにすると、USIエンジン (tools/usi_engine.py) が
  書き出すsyncファイルを追従して自動検索する。単体利用時はOFFのまま。
"""

import json
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kifudb.board import Position, normalize_sfen_main, usi_to_move16  # noqa: E402
from kifudb.export import game_to_csa, safe_filename  # noqa: E402
from kifudb.ki2 import format_pv_ki2, move16_to_ki2  # noqa: E402
from kifudb.query import (DEFAULT_PAGE_SIZE as PAGE_SIZE,  # noqa: E402
                          PrecedentReader, REASON_JA,
                          compute_source_intervals, format_report,
                          tournament_label)
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

# Xポスト文言のテンプレート。runtime/post_template.txt を編集すれば
# 再起動なしで反映される (ファイルがない時にこの内容で自動生成。
# デフォルトに戻したい時はファイルを削除する)。
# 使える変数: {tournament} {black} {white} {date} {ply} {next_move}
#             {result} {reason} {ply_count} {source} {event} {url}
POST_TEMPLATE_FILE = RUNTIME_DIR / "post_template.txt"
DEFAULT_POST_TEMPLATE = """{tournament}

{date}　{black} - {white}
{ply}手目 {next_move}

{url}"""


def load_post_template() -> str:
    try:
        return POST_TEMPLATE_FILE.read_text(encoding="utf-8")
    except OSError:
        try:
            POST_TEMPLATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            POST_TEMPLATE_FILE.write_text(DEFAULT_POST_TEMPLATE, encoding="utf-8")
        except OSError:
            pass
        return DEFAULT_POST_TEMPLATE


class PrecedentViewer:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("前例ビューア (kifudb)")
        root.geometry("900x810")

        self.precedents = []
        self.query_position: Position | None = None
        self._total_games = 0
        self._sync_mtime: float | None = None
        self._search_running = False
        self._sync_file = DEFAULT_SYNC_FILE  # runtime/gui_config.json で上書き可
        self._reader: PrecedentReader | None = None
        # 出典絞り込み用の島表 (query.py参照) の先読み管理
        self._interval_cache: dict[str, tuple] = {}          # db_path -> (islands, max_gid)
        self._interval_jobs: dict[str, threading.Event] = {}  # 実行中の先読み

        self._setup_fonts()
        self._build_widgets()
        self._load_config()
        if len(sys.argv) > 1:
            self.db_var.set(sys.argv[1])
        self._prime_source_intervals()  # 起動直後から先読みを始める
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

        # PanedWindowのペインは全幅に広がるため、コンテナを挟んで
        # 枠線(LabelFrame)ごと列幅に合わせて左詰めにする
        cand_holder = ttk.Frame(panes)
        cand_frame = ttk.LabelFrame(cand_holder, text="候補手")
        cand_frame.pack(side=tk.LEFT, fill=tk.Y)
        cand_cols = ("no", "move", "count", "black", "white", "draw", "rate",
                     "confl")
        self.cand_tv = ttk.Treeview(cand_frame, columns=cand_cols,
                                    show="headings", height=6)
        for col, label, width, anchor in (
                ("no", "No.", 44, tk.E),
                ("move", "指し手", 96, tk.W),
                ("count", "出現", 70, tk.E), ("black", "先手勝", 70, tk.E),
                ("white", "後手勝", 70, tk.E), ("draw", "引分", 60, tk.E),
                ("rate", "先手勝率", 80, tk.E), ("confl", "合流", 60, tk.E)):
            self.cand_tv.heading(col, text=label)
            self.cand_tv.column(col, width=width, anchor=anchor, stretch=False)
        # 列幅の合計にウィジェット自体を合わせ、左に詰める
        self.cand_tv.pack(side=tk.LEFT, fill=tk.Y)
        cand_scroll = ttk.Scrollbar(cand_frame, orient=tk.VERTICAL,
                                    command=self.cand_tv.yview)
        self.cand_tv.configure(yscrollcommand=cand_scroll.set)
        cand_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        panes.add(cand_holder, weight=1)

        prec_frame = ttk.LabelFrame(panes, text="前例")

        # 出典フィルタ。切り替えるとDBから絞り込み条件付きで1ページ取り直す
        # (ロード済み分の表示切替ではない)。No. は現在の条件での新しい順の順位。
        filter_row = ttk.Frame(prec_frame)
        filter_row.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(2, 0))
        ttk.Label(filter_row, text="出典:").pack(side=tk.LEFT)
        self.source_filter_vars: dict[str, tk.BooleanVar] = {}
        for key, label in (("floodgate", "floodgate"), ("wcsc", "WCSC"),
                           ("denryusen", "電竜戦"), ("other", "その他")):
            var = tk.BooleanVar(value=True)
            self.source_filter_vars[key] = var
            ttk.Checkbutton(filter_row, text=label, variable=var,
                            command=self._on_filter_toggle).pack(
                side=tk.LEFT, padx=(6, 0))
        # 島表 (出典→game_id区間) 先読みの進捗。完了後は消える。
        self.filter_prep_var = tk.StringVar(value="")
        ttk.Label(filter_row, textvariable=self.filter_prep_var,
                  foreground="gray").pack(side=tk.RIGHT)

        tree_holder = ttk.Frame(prec_frame)
        tree_holder.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        prec_cols = ("no", "date", "black", "white", "next", "result", "reason",
                     "plies", "source")
        self.prec_tv = ttk.Treeview(tree_holder, columns=prec_cols,
                                    show="headings")
        for col, label, width, anchor in (
                ("no", "No.", 44, tk.E),
                ("date", "対局日", 90, tk.W), ("black", "先手", 170, tk.W),
                ("white", "後手", 170, tk.W), ("next", "指し手", 96, tk.W),
                ("result", "勝者", 44, tk.CENTER), ("reason", "終局理由", 68, tk.CENTER),
                ("plies", "手数", 50, tk.E), ("source", "出典", 80, tk.W)):
            self.prec_tv.heading(col, text=label)
            self.prec_tv.column(col, width=width, anchor=anchor,
                                stretch=col in ("black", "white"))
        scroll = ttk.Scrollbar(tree_holder, orient=tk.VERTICAL,
                               command=self.prec_tv.yview)
        self.prec_tv.configure(yscrollcommand=scroll.set)
        self.prec_tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.prec_tv.bind("<<TreeviewSelect>>", self._on_precedent_select)
        self.prec_tv.bind("<Double-1>", self._on_precedent_open)
        panes.add(prec_frame, weight=3)

        detail_frame = ttk.LabelFrame(panes, text="詳細 (評価値・読み筋・URL)")
        detail_buttons = ttk.Frame(detail_frame)
        detail_buttons.pack(side=tk.RIGHT, fill=tk.Y, padx=4, pady=4)
        self.copy_url_button = ttk.Button(detail_buttons, text="URLコピー",
                                          command=self._copy_url,
                                          state=tk.DISABLED)
        self.copy_url_button.pack(fill=tk.X)
        self.post_x_button = ttk.Button(detail_buttons, text="Xでポスト",
                                        command=self._post_to_x,
                                        state=tk.DISABLED)
        self.post_x_button.pack(fill=tk.X, pady=(4, 0))
        self.copy_kifu_button = ttk.Button(detail_buttons, text="棋譜コピー",
                                           command=self._copy_kifu,
                                           state=tk.DISABLED)
        self.copy_kifu_button.pack(fill=tk.X, pady=(4, 0))
        self.open_file_button = ttk.Button(detail_buttons, text="ファイルで開く",
                                           command=self._open_kifu_file,
                                           state=tk.DISABLED)
        self.open_file_button.pack(fill=tk.X, pady=(4, 0))
        self.detail_text = tk.Text(detail_frame, height=5, wrap=tk.WORD,
                                   state=tk.DISABLED, font=self.mono_font)
        self.detail_text.pack(fill=tk.BOTH, expand=True)
        panes.add(detail_frame, weight=1)

        bottom = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        bottom.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="DBとsfenを指定して検索してください。")
        # 右のボタン類を先に確保し、ステータスは残り幅に収めて切り詰める
        # (長いメッセージでもボタンを画面外へ押し出さないようにする)。
        ttk.Button(bottom, text="レポート保存...",
                   command=self._save_report).pack(side=tk.RIGHT)
        self.more_button = ttk.Button(bottom, text=f"さらに{PAGE_SIZE}件表示",
                                      command=self._load_more, state=tk.DISABLED)
        self.more_button.pack(side=tk.RIGHT, padx=6)
        ttk.Label(bottom, textvariable=self.status_var, anchor=tk.W).pack(
            side=tk.LEFT, fill=tk.X, expand=True)

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
            cached = self._interval_cache.get(db_path)
            if cached:
                self._reader.set_source_intervals(*cached)
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
        self._prime_source_intervals()  # DBが変わっていたら先読みを開始
        self._search_running = True
        self.search_button.config(state=tk.DISABLED)
        self.status_var.set("検索中...")
        # フィルタ状態は tk 変数なのでメインスレッドで読み取って渡す
        threading.Thread(target=self._search_task,
                         args=(db_path, sfen, self._enabled_sources()),
                         daemon=True).start()

    def _search_task(self, db_path: str, sfen: str, sources) -> None:
        try:
            sfen_main = normalize_sfen_main(sfen)
            position = Position()
            position.set_sfen(sfen_main)
            started = time.perf_counter()
            reader = self._get_reader(db_path)
            # 候補手統計は常に全出典。前例一覧だけ出典フィルタを適用する。
            candidates, _, total = reader.lookup(sfen, max_precedents=0)
            if sources is not None:
                self._wait_interval_prep(db_path)
            precedents = reader.precedents_page(sfen, limit=PAGE_SIZE,
                                                sources=sources)
            confluence = reader.confluence_counts(sfen, candidates)
            transpositions = reader.transposition_moves(sfen, candidates)
            elapsed = (time.perf_counter() - started) * 1000
            self.root.after(0, self._show_results, position, candidates,
                            precedents, total, elapsed, confluence,
                            transpositions, sources)
        except Exception as exc:  # noqa: BLE001 - surface everything to the user
            self.root.after(0, self._show_error, str(exc))

    def _show_error(self, message: str) -> None:
        self._search_running = False
        self.search_button.config(state=tk.NORMAL)
        self.status_var.set("エラー")
        messagebox.showerror("検索エラー", message)

    def _show_results(self, position, candidates, precedents, total, elapsed,
                      confluence=None, transpositions=None,
                      sources=None) -> None:
        self._search_running = False
        self.search_button.config(state=tk.NORMAL)
        self.query_position = position
        self.precedents = []
        self._total_games = total
        confluence = confluence or {}

        self.cand_tv.delete(*self.cand_tv.get_children())
        for rank, c in enumerate(candidates, start=1):
            code = usi_to_move16(c.usi)
            label = (move16_to_ki2(position, code) if code is not None
                     else "(終局)" if c.usi == "(end)" else c.usi)
            # 引き分けは後手勝ち扱いで先手勝率を算出する
            decided = c.black_wins + c.white_wins + c.draws
            rate = f"{c.black_wins / decided * 100:.1f}%" if decided else "-"
            merged = confluence.get(c.usi, 0)
            self.cand_tv.insert("", tk.END, values=(
                rank, label, c.game_count, c.black_wins, c.white_wins,
                c.draws, rate, merged or ""))
        # 前例ゼロだが指すと既存対局に合流する手 (擬似合法手ベース)。
        # 出現・勝率は空欄にし、合流数だけを表示する。
        for usi, merged in (transpositions or []):
            code = usi_to_move16(usi)
            label = move16_to_ki2(position, code) if code is not None else usi
            self.cand_tv.insert("", tk.END, values=(
                "", label, "", "", "", "", "", merged))

        self.prec_tv.delete(*self.prec_tv.get_children())
        self._append_precedents(precedents)

        self._set_detail("前例を選択すると評価値・読み筋・URLを表示します。")
        self.status_var.set(
            f"前例 {total}局 / 候補手 {len(candidates)}種 / "
            f"表示 {len(self.precedents)}件 ({elapsed:.1f}ms)")
        self._refetch_if_filter_changed(sources)

    def _prime_source_intervals(self) -> None:
        """出典絞り込み用の島表をバックグラウンドで先読みする (DBごとに一度)。

        起動直後と検索開始時に呼ばれる。専用の読み取り接続で走るため、検索
        など他の処理をブロックしない。失敗した場合は記録を消し、次の機会に
        再試行する (絞り込み自体は検索スレッドの同期計算でも正しく動く)。"""
        db_path = self.db_var.get().strip()
        if (not db_path or db_path in self._interval_cache
                or db_path in self._interval_jobs
                or not Path(db_path).is_file()):
            return
        event = threading.Event()
        self._interval_jobs[db_path] = event

        def progress(done, total):
            pct = min(done * 100 // max(total, 1), 99)
            self.root.after(0, self.filter_prep_var.set, f"絞り込み準備 {pct}%")

        def task():
            try:
                intervals, max_gid = compute_source_intervals(
                    db_path, progress)
                if max_gid is not None:
                    self._interval_cache[db_path] = (intervals, max_gid)
                    # 属性の設定のみで接続は触らないためスレッドから直接
                    # 入れてよい。event を待つ検索スレッドが即座に使える。
                    reader = self._reader
                    if reader is not None and reader.db_path == db_path:
                        reader.set_source_intervals(intervals, max_gid)
            except Exception:  # noqa: BLE001 - 先読み失敗は同期計算で賄える
                pass
            finally:
                self._interval_jobs.pop(db_path, None)
                event.set()
                self.root.after(0, self.filter_prep_var.set, "")

        threading.Thread(target=task, daemon=True).start()

    def _wait_interval_prep(self, db_path: str) -> None:
        """島表の先読みが進行中なら完了を待つ (検索スレッド用)。

        待たずに同期計算すると同じ走査が二重に走り、I/Oを取り合って両方
        遅くなるため、進行中の結果を使い回す。"""
        event = self._interval_jobs.get(db_path)
        if event is not None:
            event.wait()

    def _enabled_sources(self):
        """有効な出典キーの集合。全てONなら None (絞り込み無しの高速経路)。"""
        enabled = frozenset(k for k, v in self.source_filter_vars.items()
                            if v.get())
        return None if len(enabled) == len(self.source_filter_vars) else enabled

    def _on_filter_toggle(self) -> None:
        """出典チェック変更: 現在の局面をフィルタ条件付きで取り直す。

        取得中の変更は、完了時の _refetch_if_filter_changed が拾い直す。"""
        if self.query_position is None or self._search_running:
            return
        self._refetch_precedents()

    def _refetch_if_filter_changed(self, sources_used) -> None:
        """取得中にチェックが変わっていたら現在の状態で取り直す。"""
        if self.query_position is not None and \
                self._enabled_sources() != sources_used:
            self._refetch_precedents()

    def _refetch_precedents(self) -> None:
        db_path = self.db_var.get().strip()
        sfen = self.sfen_var.get().strip()
        if not db_path or not sfen:
            return
        sources = self._enabled_sources()
        self._search_running = True
        self.search_button.config(state=tk.DISABLED)
        self.more_button.config(state=tk.DISABLED)
        self.status_var.set("絞り込みを反映中...")

        def task():
            try:
                started = time.perf_counter()
                if sources is not None:
                    self._wait_interval_prep(db_path)
                page = self._get_reader(db_path).precedents_page(
                    sfen, limit=PAGE_SIZE, sources=sources)
                elapsed = (time.perf_counter() - started) * 1000
                self.root.after(0, self._show_refetch, page, sources, elapsed)
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, self._show_error, str(exc))

        threading.Thread(target=task, daemon=True).start()

    def _show_refetch(self, page, sources_used, elapsed) -> None:
        self._search_running = False
        self.search_button.config(state=tk.NORMAL)
        self.precedents = []
        self.prec_tv.delete(*self.prec_tv.get_children())
        self._append_precedents(page)
        self.status_var.set(
            f"前例 {self._total_games}局 / 表示 {len(self.precedents)}件 "
            f"({elapsed:.0f}ms)")
        self._refetch_if_filter_changed(sources_used)

    def _insert_prec_row(self, index: int, p) -> None:
        """1件を表に挿入する。iid は self.precedents 内の添字、No. (p.rank)
        は現在の絞り込み条件での新しい順の順位。"""
        code = usi_to_move16(p.next_move_usi) if p.next_move_usi else None
        next_label = (move16_to_ki2(self.query_position, code)
                      if code is not None else "(終局)")
        self.prec_tv.insert("", tk.END, iid=str(index), values=(
            p.rank, p.started_at[:10].replace("-", "/"),
            p.black_name, p.white_name, next_label,
            WINNER_JA.get(p.result, "-"),
            REASON_JA.get(p.end_reason, p.end_reason),
            p.ply_count, p.source))

    def _append_precedents(self, page) -> None:
        """Append one page of precedents to the table (used by search & 続き)."""
        start = len(self.precedents)
        for offset, p in enumerate(page):
            self._insert_prec_row(start + offset, p)
        self.precedents.extend(page)
        # ページが満杯 = まだ続きがある可能性が高い
        self.more_button.config(
            state=tk.NORMAL if len(page) == PAGE_SIZE else tk.DISABLED)

    def _load_more(self) -> None:
        if self._search_running or not self.precedents:
            return
        last = self.precedents[-1]
        before = (last.sort_key, last.game_id)
        start_rank = last.rank
        db_path = self.db_var.get().strip()
        sfen = self.sfen_var.get().strip()
        sources = self._enabled_sources()
        self._search_running = True
        self.more_button.config(state=tk.DISABLED)
        self.status_var.set("続きを取得中...")

        def task():
            try:
                if sources is not None:
                    self._wait_interval_prep(db_path)
                page = self._get_reader(db_path).precedents_page(
                    sfen, limit=PAGE_SIZE, before=before,
                    sources=sources, start_rank=start_rank)
                self.root.after(0, self._show_more, page, sources)
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, self._show_error, str(exc))

        threading.Thread(target=task, daemon=True).start()

    def _show_more(self, page, sources_used=None) -> None:
        self._search_running = False
        self._append_precedents(page)
        self.status_var.set(
            f"前例 {self._total_games}局 / 表示 {len(self.precedents)}件")
        self._refetch_if_filter_changed(sources_used)

    def _on_precedent_select(self, _event=None) -> None:
        p = self._selected_precedent()
        if p is None:
            return
        button_state = tk.NORMAL if p.url else tk.DISABLED
        self.copy_url_button.config(state=button_state)
        self.post_x_button.config(state=button_state)
        self.copy_kifu_button.config(state=tk.NORMAL)
        self.open_file_button.config(state=tk.NORMAL)
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
            lines.append(f"URL: {p.url}")
        lines.append("(ダブルクリック: 棋譜ビューアをブラウザで開く。"
                     "「棋譜コピー」でShogiHome用にクリップボードへコピー)")
        self._set_detail("\n".join(lines))

    def _kifu_text(self, game_id: int) -> tuple[str, str]:
        """棋譜テキストを取得。元ファイルが残っていればそれを優先する
        (消費時間やコメントなど情報が多いため)。なければDBから復元する。
        戻り値: (テキスト, 出所の説明)"""
        reader = self._get_reader(self.db_var.get().strip())
        source = reader.get_source_path(game_id)
        if source and Path(source).is_file():
            raw = Path(source).read_bytes()
            if source.lower().endswith((".kif", ".kifu")):
                from kifudb.kif import decode_kif_bytes
                return decode_kif_bytes(raw), f"元ファイル: {Path(source).name}"
            from kifudb.csa import decode_bytes
            return decode_bytes(raw), f"元ファイル: {Path(source).name}"
        detail = reader.get_game(game_id)
        return game_to_csa(detail), "DBから復元"

    def _on_precedent_open(self, _event=None) -> None:
        """ダブルクリック: 棋譜ビューアをブラウザで開く。

        URLを持たない前例 (棋譜ビューア非対応の大会等) の場合は、代わりに
        棋譜をクリップボードへコピーする (ShogiHome用) フォールバックを行う。"""
        p = self._selected_precedent()
        if p is None:
            return
        if p.url:
            self._open_url(p.url)
            self.status_var.set("棋譜ビューアをブラウザで開きました。")
            return
        self._copy_kifu()

    def _copy_kifu(self) -> None:
        """棋譜をクリップボードへコピーする。

        ShogiHomeは検討中のウィンドウに Ctrl+V (⌘V) で貼り付けられる。
        ファイル関連付けで開くと必ず新しいウィンドウが立つため、
        同じウィンドウで続けるにはこの方式が最短。"""
        p = self._selected_precedent()
        if p is None:
            return
        try:
            text, origin = self._kifu_text(p.game_id)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status_var.set(
                f"棋譜をコピーしました ({origin})。ShogiHomeで Ctrl+V / ⌘V で開けます。")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("棋譜取得エラー", str(exc))

    def _open_kifu_file(self) -> None:
        """棋譜をファイルとして既定アプリで開く (新しいウィンドウになる)。"""
        p = self._selected_precedent()
        if p is None:
            return
        try:
            reader = self._get_reader(self.db_var.get().strip())
            source = reader.get_source_path(p.game_id)
            if source and Path(source).is_file():
                self._open_local_file(Path(source))
                self.status_var.set(f"元ファイルを開きました: {Path(source).name}")
                return
            detail = reader.get_game(p.game_id)
            out_dir = RUNTIME_DIR / "exported"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{safe_filename(detail.event)}.csa"
            path.write_text(game_to_csa(detail), encoding="utf-8")
            self._open_local_file(path)
            self.status_var.set(f"棋譜を復元して開きました: {path.name}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("棋譜取得エラー", str(exc))

    @staticmethod
    def _open_local_file(path: Path) -> None:
        PrecedentViewer._open_via_os(str(path))

    @staticmethod
    def _open_url(url: str) -> None:
        # webbrowser.open は macOS で 2 タブ開くことがあるため OS の open を直呼び。
        PrecedentViewer._open_via_os(url)

    @staticmethod
    def _open_via_os(target: str) -> None:
        if sys.platform == "win32":
            import os
            os.startfile(target)  # noqa: S606
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", target])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", target])

    def _selected_precedent(self):
        selection = self.prec_tv.selection()
        if not selection or not self.precedents:
            return None
        try:
            return self.precedents[int(selection[0])]
        except (ValueError, IndexError):
            return None

    def _copy_url(self) -> None:
        p = self._selected_precedent()
        if p is None or not p.url:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(p.url)
        self.status_var.set("URLをコピーしました。")

    def _post_to_x(self) -> None:
        p = self._selected_precedent()
        if p is None or not p.url:
            return
        code = usi_to_move16(p.next_move_usi) if p.next_move_usi else None
        context = {
            "tournament": tournament_label(p.source, p.event),
            "black": p.black_name, "white": p.white_name,
            "date": p.started_at[:10].replace("-", "/"),
            "ply": p.ply + 1,  # 次の一手の手数
            "next_move": (move16_to_ki2(self.query_position, code)
                          if code is not None and self.query_position else "(終局)"),
            "result": RESULT_JA.get(p.result, "不明"),
            "reason": REASON_JA.get(p.end_reason, p.end_reason),
            "ply_count": p.ply_count,
            "source": p.source, "event": p.event, "url": p.url,
        }
        try:
            text = load_post_template().format(**context)
        except (KeyError, ValueError) as exc:
            messagebox.showerror(
                "テンプレートエラー",
                f"post_template.txt を確認してください: {exc}\n"
                f"使える変数: {', '.join('{' + k + '}' for k in context)}")
            return
        self._open_url("https://x.com/intent/post?text="
                       + urllib.parse.quote(text))

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
