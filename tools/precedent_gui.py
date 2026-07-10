#!/usr/bin/env python3
"""前例ビューア (テストGUI): sfenを貼り付けて前例DBを検索する。

起動:  python3 tools/precedent_gui.py [db_path]

- sfen欄には "position sfen ...", "sfen ...", "startpos", 素のsfen の
  いずれを貼り付けてもよい。手数部分は無視される。
- 前例をダブルクリックすると棋譜ビューアをブラウザで開く (URLが無い前例は
  棋譜のクリップボードコピーにフォールバック)。
  「棋譜コピー」ボタンは棋譜をクリップボードにコピーする。将棋盤GUIの検討中
  ウィンドウに Ctrl+V (⌘V) でそのまま貼り付けられる (元ファイルが残っていれば
  その内容を、なければDBから復元した棋譜を使う)。
  「ファイルで開く」ボタンは既定アプリで開く (新規ウィンドウ)。
- 前例を選択すると、その局面での評価値と読み筋(記録があれば)を表示する。
- 「将棋盤GUIの局面を追従」(既定ON) は、USIエンジン (tools/usi_engine.py) が
  書き出すsyncファイルを追従して自動検索する。単体利用時はOFFにしてよい。
"""

import json
import queue
import random
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
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
from kifudb.db import open_read_only  # noqa: E402
from kifudb.floodgate import update_once  # noqa: E402
from kifudb.ingest import (KIFU_SUFFIXES, detect_source,  # noqa: E402
                           ingest_folder)
from kifudb.query import extend_source_intervals  # noqa: E402
from kifudb.sfen_ingest import (delete_batch, ingest_file,  # noqa: E402
                                list_batches, scan_file)
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
# floodgate逐次更新の一時保存フォルダ (取り込み後に削除される。詳細は
# kifudb/floodgate.py)。未終局・エラーのファイルだけが一時的に残る。
FLOODGATE_MIRROR = Path(__file__).resolve().parent.parent / "data" / "floodgate"
# .sfen 連続対局 (水匠定跡生成の出力) 専用DBの既定パス。実対局の前例DBとは分けて管理する
# (統計の意味が異なる上、バッチ削除の誤爆半径を隔離するため)。
DEFAULT_SFEN_DB = Path(__file__).resolve().parent.parent / "data" / "sfen.db"
# 個人入手棋譜 (公開されていない csa/kif) の既定DB。誰でも入手できる公開棋譜
# (floodgate/WCSC/電竜戦) は公開前例DBに集め、それ以外はこちらに自動振り分け
# する — 公開DBを配布しても私的な棋譜が混ざらないようにするため。
DEFAULT_PRIVATE_DB = Path(__file__).resolve().parent.parent / "data" / "private.db"
# 自動更新の実行タイミング: 毎時この分を窓の開始とし、ジッタ秒を足して実行。
# :20/:50 + 0〜300秒 = 対局開始 (:00/:30) の5〜10分前のどこか。全利用者が
# 同一秒にwdoorへ殺到しないよう、起動ごとのランダムなずれで分散させる。
AUTO_UPDATE_MINUTES = (20, 50)
AUTO_UPDATE_JITTER = 300
# 島表 (出典→game_id区間) の永続キャッシュ。DBが変わらない限り起動時の
# 再計算 (gamesテーブル全走査) を丸ごと省ける。
INTERVALS_CACHE_PATH = RUNTIME_DIR / "source_intervals.json"


def load_interval_sidecar(db_path: str):
    """前回起動時に計算した島表を、検証付きで読み込む。

    DBの MAX(game_id) と総対局数が保存時と一致する場合だけ採用する
    (増分取り込みや再構築があれば不一致になり、呼び出し側が再計算する)。
    戻り値: (intervals, max_gid, count) または None。"""
    try:
        data = json.loads(INTERVALS_CACHE_PATH.read_text(encoding="utf-8"))
        entry = data[str(Path(db_path).resolve())]
        conn = open_read_only(db_path)
        try:
            max_gid, count = conn.execute(
                "SELECT MAX(game_id), COUNT(*) FROM games").fetchone()
        finally:
            conn.close()
        if entry["max_gid"] != max_gid or entry["count"] != count:
            return None
        return [tuple(iv) for iv in entry["intervals"]], max_gid, count
    except Exception:  # noqa: BLE001 - 壊れた/無いキャッシュは再計算で賄う
        return None


def save_interval_sidecar(db_path: str, intervals, max_gid: int,
                          count: int) -> None:
    try:
        try:
            data = json.loads(INTERVALS_CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[str(Path(db_path).resolve())] = {
            "max_gid": max_gid, "count": count,
            "intervals": [list(iv) for iv in intervals]}
        INTERVALS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        INTERVALS_CACHE_PATH.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 保存失敗は次回再計算するだけ
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
        root.title("KiriCompass")
        root.geometry("900x810")

        self.precedents = []
        self.query_position: Position | None = None
        self._total_games = 0
        self._sync_mtime: float | None = None
        self._search_running = False
        self._sync_file = DEFAULT_SYNC_FILE  # runtime/gui_config.json で上書き可
        self._reader: PrecedentReader | None = None
        # 出典絞り込み用の島表 (query.py参照) の先読み管理
        self._interval_cache: dict[str, tuple] = {}   # db_path -> (islands, max_gid, count)
        self._interval_jobs: dict[str, threading.Event] = {}  # 実行中の先読み
        # DB更新 (フォルダ取り込み / floodgate)。書き込みジョブは1本のキューで
        # 直列化し、決して同時にDBへ書かない。読み取り (検索) とはWALで共存。
        self._db_jobs: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._db_job_running: str | None = None
        self._floodgate_queued = False   # floodgateジョブの重複投入防止
        self._auto_update_after: str | None = None  # スケジューラのafter ID
        self._auto_jitter = random.randint(0, AUTO_UPDATE_JITTER)  # 群れ分散
        self._update_win: tk.Toplevel | None = None
        self._update_log_lines: list[str] = []  # ウィンドウ再表示用の履歴

        self._setup_fonts()
        self._build_widgets()
        self._load_config()
        if len(sys.argv) > 1:
            self.db_var.set(sys.argv[1])
        self._prime_source_intervals()  # 起動直後から先読みを始める
        threading.Thread(target=self._db_worker, daemon=True).start()
        self._schedule_auto_update()
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
        ttk.Button(top, text="DB更新...", command=self._open_update_window).grid(
            row=0, column=3, padx=(4, 0))
        self.auto_update_var = tk.BooleanVar(value=False)
        self.sfen_db_var = tk.StringVar(value=str(DEFAULT_SFEN_DB))
        # floodgate更新・公開棋譜の取り込み先DB。ビューアの表示DBに追従させると
        # .sfen DB等を開いている間の自動サイクルが誤爆するため、固定で持つ。
        self.fg_db_var = tk.StringVar(value="")
        self.private_db_var = tk.StringVar(value=str(DEFAULT_PRIVATE_DB))

        ttk.Label(top, text="SFEN:").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        self.sfen_var = tk.StringVar()
        sfen_entry = ttk.Entry(top, textvariable=self.sfen_var,
                               font=self.mono_font)
        sfen_entry.grid(row=1, column=1, sticky=tk.EW, padx=4, pady=(6, 0))
        sfen_entry.bind("<Return>", lambda _e: self.search())
        self.search_button = ttk.Button(top, text="検索", command=self.search)
        self.search_button.grid(row=1, column=2, pady=(6, 0))

        # 追従チェックは検索ボタンの右
        self.sync_var = tk.BooleanVar(value=True)  # 既定で追従ON
        sync_check = ttk.Checkbutton(
            top, text="将棋盤GUIの局面を追従",
            variable=self.sync_var, command=self._on_sync_toggle)
        sync_check.grid(row=1, column=3, sticky=tk.W, padx=(6, 0), pady=(6, 0))
        top.columnconfigure(1, weight=1)

        # DB行・SFEN行をボタンひとつで収納/展開 (ボタン自体は常時見える)
        self._top_collapsibles = list(top.grid_slaves())
        self._top_collapsed = False
        self._collapse_btn = ttk.Button(top, text="▲", width=2,
                                        command=self._toggle_top_rows)
        self._collapse_btn.grid(row=0, column=4, rowspan=2,
                                sticky=tk.NE, padx=(6, 0))

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
        # 3系統DB (csa / private / .sfen) のワンクリック切替を候補手の右に
        switch = ttk.Frame(cand_holder)
        switch.pack(side=tk.LEFT, anchor=tk.N, padx=(10, 0), pady=4)
        for text_, kind in (("csa", "public"), ("private", "private"),
                            (".sfen", "sfen")):
            ttk.Button(switch, text=text_, width=7,
                       command=lambda k=kind: self._switch_db(k)).pack(
                fill=tk.X, pady=1)
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
        # DB更新の稼働表示 (自動更新ON時に「次回 :55」等を控えめに出す)
        self.update_status_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.update_status_var,
                  foreground="gray").pack(side=tk.RIGHT, padx=(0, 8))
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
                self._reader.set_source_intervals(cached[0], cached[1])
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
                # 前回起動時の結果が有効ならそれを使い、全走査を省く
                cached = load_interval_sidecar(db_path)
                if cached is not None:
                    intervals, max_gid, count = cached
                else:
                    intervals, max_gid, count = compute_source_intervals(
                        db_path, progress)
                    if max_gid is not None:
                        save_interval_sidecar(db_path, intervals,
                                              max_gid, count)
                if max_gid is not None:
                    self._interval_cache[db_path] = (intervals, max_gid, count)
                    # 属性の設定のみで接続は触らないためスレッドから直接
                    # 入れてよい。event を待つ検索スレッドが即座に使える。
                    reader = self._reader
                    if reader is not None and reader.db_path == db_path:
                        reader.set_source_intervals(intervals, max_gid)
            except Exception:  # noqa: BLE001 - 先読み失敗は同期計算で賄える
                pass
            finally:
                # 例外時も必ず event を立て、待機側の永久フリーズを防ぐ
                self._interval_jobs.pop(db_path, None)
                event.set()
                self.root.after(0, self.filter_prep_var.set, "")

        try:
            threading.Thread(target=task, daemon=True).start()
        except RuntimeError:
            # スレッドが生成できなければジョブ登録を取り消して待機を解放
            self._interval_jobs.pop(db_path, None)
            event.set()

    def _wait_interval_prep(self, db_path: str) -> None:
        """島表の先読みが進行中なら完了を待つ (検索スレッド用)。

        待たずに同期計算すると同じ走査が二重に走り、I/Oを取り合って両方
        遅くなるため、進行中の結果を使い回す。event は finally で必ず立つが、
        遅い外付けドライブ等で長引く場合に備えタイムアウトを置く — 切れても
        検索スレッド側の同期計算にフォールバックするだけで、正しく動く。"""
        event = self._interval_jobs.get(db_path)
        if event is not None:
            event.wait(timeout=15.0)

    # -- DB更新 (フォルダ取り込み / floodgate逐次更新) ---------------------

    def _db_worker(self) -> None:
        """書き込みジョブを直列実行するワーカー (デーモンスレッド)。

        フォルダ取り込みと floodgate 更新はすべてこのキューを通るので、
        DBへ同時に書くジョブは存在しない。検索 (読み取り) とはWALで共存。"""
        while True:
            kind, fn = self._db_jobs.get()
            if kind == "floodgate":
                self._floodgate_queued = False
            self._db_job_running = kind
            self.root.after(0, self._set_update_status)
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - 次のジョブは続行する
                self._ulog(f"[{kind}] 失敗: {exc}")
            finally:
                self._db_job_running = None
                self.root.after(0, self._set_update_status)

    def _toggle_top_rows(self) -> None:
        """DB行・SFEN行を収納/展開する (盤面GUI追従で使う際の省スペース化)。

        grid_remove は配置設定を覚えたまま隠すので、再表示で元に戻る。"""
        self._top_collapsed = not self._top_collapsed
        for widget in self._top_collapsibles:
            if self._top_collapsed:
                widget.grid_remove()
            else:
                widget.grid()
        self._collapse_btn.config(text="▼" if self._top_collapsed else "▲")

    def _switch_db(self, kind: str) -> None:
        """csa.db / private.db / sfen.db をワンクリックで切り替える。

        切り替え時は出典フィルタを全てONに戻し、現在のsfenで表示を更新する。"""
        var = {"public": self.fg_db_var, "private": self.private_db_var,
               "sfen": self.sfen_db_var}[kind]
        db_path = var.get().strip()
        if not db_path or not Path(db_path).is_file():
            messagebox.showerror(
                "エラー", f"切り替え先のDBがありません: {db_path or '(未設定)'}\n"
                          "「DB更新...」ウィンドウでパスを設定してください。",
                parent=self.root)
            return
        self.db_var.set(db_path)
        for filter_var in self.source_filter_vars.values():
            filter_var.set(True)
        self._prime_source_intervals()
        if self.sfen_var.get().strip() and not self._search_running:
            self.search()

    def _dialog_parent(self):
        return (self._update_win if self._update_win is not None
                and self._update_win.winfo_exists() else self.root)

    def _db_has_sfen_games(self, db_path: str) -> bool:
        """.sfen連続対局 (source='sfen') が入っているか。"""
        try:
            conn = open_read_only(db_path)
            try:
                return conn.execute("SELECT 1 FROM games WHERE source='sfen' "
                                    "LIMIT 1").fetchone() is not None
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return False

    def _floodgate_target_db(self, quiet: bool = False) -> str | None:
        """floodgate更新の対象DB (固定設定)。無効なら通知して None。"""
        db_path = self.fg_db_var.get().strip()
        if not db_path or not Path(db_path).is_file():
            message = "floodgate更新の対象DBが未指定か存在しません"
            if quiet:
                self._ulog(f"[floodgate] {message} のためスキップ")
            else:
                messagebox.showerror(
                    "エラー", f"{message}。DB更新ウィンドウで指定してください。",
                    parent=self._dialog_parent())
            return None
        if self._db_has_sfen_games(db_path):
            message = ("対象DBに.sfen連続対局が入っています。floodgateの"
                       "取り込み先は実対局の前例DBにしてください")
            if quiet:
                self._ulog(f"[floodgate] {message} のためスキップ")
            else:
                messagebox.showerror("エラー", message,
                                     parent=self._dialog_parent())
            return None
        return db_path

    def _enqueue_floodgate(self, label: str, days: int = 2,
                           quiet: bool = False) -> None:
        if self._floodgate_queued:
            self._ulog(f"[floodgate] 既にジョブが待機中のためスキップ ({label})")
            return
        db_path = self._floodgate_target_db(quiet=quiet)
        if db_path is None:
            return
        self._floodgate_queued = True

        def job():
            self._ulog(f"[floodgate] 更新開始 ({label}, 過去{days}日分を照合)")
            update_once(db_path, FLOODGATE_MIRROR, days=days,
                        log_line=lambda m: self._ulog(f"[floodgate] {m}"))
            self._after_db_update(db_path)

        self._db_jobs.put(("floodgate", job))

    def _enqueue_folder_ingest(self) -> None:
        """フォルダ内の棋譜を公開/プライベートに自動振り分けて取り込む。

        振り分けはファイル名 (stem) の出典判定: wdoor/WCSC/電竜戦と判定できる
        ものは公開前例DBへ、それ以外はプライベートDBへ。公開DBを配布しても
        私的な棋譜が混ざらないようにするための既定動作。"""
        public_db = self._floodgate_target_db()  # 検証込み (sfen混入も拒否)
        if public_db is None:
            return
        private_db = self.private_db_var.get().strip() or str(DEFAULT_PRIVATE_DB)
        if self._db_has_sfen_games(private_db):
            messagebox.showerror(
                "エラー", "プライベートDBに.sfen連続対局が入っています。"
                          "別のパスを指定してください。",
                parent=self._dialog_parent())
            return
        folder = filedialog.askdirectory(
            title="取り込む棋譜フォルダを選択", parent=self._dialog_parent())
        if not folder:
            return

        def is_public(p: Path) -> bool:
            return detect_source(p.stem) != "other"

        def job():
            self._ulog(f"[取り込み] 開始: {folder}")
            files = [p for p in Path(folder).rglob("*")
                     if p.suffix.lower() in KIFU_SUFFIXES and p.is_file()]
            n_public = sum(1 for p in files if is_public(p))
            n_private = len(files) - n_public
            self._ulog(f"[取り込み] 振り分け: 公開 {n_public} / "
                       f"プライベート {n_private}")
            if n_public:
                stats = ingest_folder(
                    public_db, folder, file_filter=is_public,
                    progress=lambda i, n: self._ulog(f"[取り込み 公開] {i}/{n}"))
                self._ulog(f"[取り込み 公開DB] {stats.summary()}")
                self._after_db_update(public_db)
            if n_private:
                stats = ingest_folder(
                    private_db, folder,
                    file_filter=lambda p: not is_public(p),
                    progress=lambda i, n: self._ulog(f"[取り込み 私] {i}/{n}"))
                self._ulog(f"[取り込み プライベートDB] {stats.summary()}")
                self._after_db_update(private_db)
            if not files:
                self._ulog("[取り込み] 対象ファイルがありません")

        self._db_jobs.put(("フォルダ取り込み", job))

    def _after_db_update(self, db_path: str) -> None:
        """取り込み後の島表の差分更新 (workerスレッドで実行)。

        追加分 (旧max_gid以降) だけ走査して島表にマージするので、逐次更新の
        たびに全走査へ戻らない。先読み前なら何もしない (次の絞り込みで計算)。"""
        cached = self._interval_cache.get(db_path)
        if not cached:
            return
        intervals, max_gid, count = cached
        try:
            new_iv, new_mg, added = extend_source_intervals(
                db_path, intervals, max_gid)
        except Exception:  # noqa: BLE001 - 失敗時は次回フル再計算に任せる
            self._interval_cache.pop(db_path, None)
            return
        if added:
            self._interval_cache[db_path] = (new_iv, new_mg, count + added)
            save_interval_sidecar(db_path, new_iv, new_mg, count + added)
            reader = self._reader
            if reader is not None and reader.db_path == db_path:
                reader.set_source_intervals(new_iv, new_mg)

    # -- 自動更新スケジューラ (毎時 :25/:55、壁時計アンカー) ----------------

    def _schedule_auto_update(self) -> None:
        if self._auto_update_after is not None:
            self.root.after_cancel(self._auto_update_after)
            self._auto_update_after = None
        if not self.auto_update_var.get():
            self._set_update_status()
            return
        now = datetime.now()
        hour_base = now.replace(minute=0, second=0, microsecond=0)
        jitter = timedelta(seconds=self._auto_jitter)
        candidates = [hour_base + timedelta(hours=h, minutes=m) + jitter
                      for h in (0, 1) for m in AUTO_UPDATE_MINUTES]
        self._auto_next_time = min(t for t in candidates if t > now)
        delay_ms = max(int((self._auto_next_time - now).total_seconds() * 1000),
                       1000)
        self._auto_update_after = self.root.after(delay_ms,
                                                  self._auto_update_fire)
        self._set_update_status()

    def _auto_update_fire(self) -> None:
        self._auto_update_after = None
        self._enqueue_floodgate("自動", quiet=True)
        self._schedule_auto_update()

    def _on_auto_update_toggle(self) -> None:
        self._save_config()
        self._schedule_auto_update()

    def _set_update_status(self) -> None:
        parts = []
        if self._db_job_running:
            parts.append(f"DB更新中: {self._db_job_running}")
        if self.auto_update_var.get() and self._auto_update_after is not None:
            parts.append(f"自動更新 次回 {self._auto_next_time:%H:%M}")
        self.update_status_var.set(" / ".join(parts))

    def _ulog(self, message: str) -> None:
        """更新ログ1行 (どのスレッドからでも呼べる)。"""
        line = f"{datetime.now():%H:%M:%S} {message}"

        def append():
            self._update_log_lines.append(line)
            del self._update_log_lines[:-500]
            widget = getattr(self, "_update_log_text", None)
            if widget is not None and widget.winfo_exists():
                widget.config(state=tk.NORMAL)
                widget.insert(tk.END, line + "\n")
                widget.see(tk.END)
                widget.config(state=tk.DISABLED)

        self.root.after(0, append)

    # -- DB更新ウィンドウ ---------------------------------------------------

    def _open_update_window(self) -> None:
        if self._update_win is not None and self._update_win.winfo_exists():
            self._update_win.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("DB更新")
        win.geometry("700x820")
        self._update_win = win

        db_frame = ttk.LabelFrame(win, text="取り込み先", padding=8)
        db_frame.pack(fill=tk.X, padx=8, pady=(8, 4))

        def db_row(label_text, var, title):
            row = ttk.Frame(db_frame)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=label_text, width=14).pack(side=tk.LEFT)
            ttk.Entry(row, textvariable=var).pack(
                side=tk.LEFT, fill=tk.X, expand=True, padx=4)

            def browse():
                chosen = filedialog.askopenfilename(
                    title=title, parent=win,
                    filetypes=[("SQLite DB", "*.db *.sqlite"), ("All", "*")])
                if chosen:
                    var.set(chosen)
                    self._save_config()

            ttk.Button(row, text="参照...", command=browse).pack(side=tk.RIGHT)

        db_row("公開前例DB:", self.fg_db_var, "公開前例DBを選択")
        db_row("プライベートDB:", self.private_db_var, "プライベートDBを選択")
        db_row(".sfen DB:", self.sfen_db_var, ".sfen 専用DBを選択")
        ttk.Label(db_frame, foreground="gray", wraplength=630, justify=tk.LEFT,
                  text="公開前例DB = 誰でも入手できる棋譜 (floodgate/WCSC/電竜戦)。"
                       "配布・共有できる状態を保つため、個人入手の棋譜は"
                       "プライベートDBへ、.sfen連続対局は .sfen DBへ入り、"
                       "互いに混ざらない。").pack(anchor=tk.W, pady=(4, 0))

        folder_frame = ttk.LabelFrame(win, text="フォルダ取り込み", padding=8)
        folder_frame.pack(fill=tk.X, padx=8, pady=4)
        ttk.Button(folder_frame, text="フォルダを選択して振り分け取り込み...",
                   command=self._enqueue_folder_ingest).pack(anchor=tk.W)
        ttk.Label(folder_frame,
                  text="フォルダ内の棋譜 (サブフォルダ含む) を増分登録します。"
                       "ファイル名から wdoor/WCSC/電竜戦 と判定できるものは"
                       "公開前例DBへ、それ以外はプライベートDBへ。再実行しても安全です。",
                  foreground="gray", wraplength=630, justify=tk.LEFT).pack(
            anchor=tk.W, pady=(4, 0))

        fg_frame = ttk.LabelFrame(win, text="floodgate 逐次更新 (公開前例DBへ)",
                                  padding=8)
        fg_frame.pack(fill=tk.X, padx=8, pady=4)
        minutes = "/".join(f":{m:02d}" for m in AUTO_UPDATE_MINUTES)
        ttk.Checkbutton(
            fg_frame,
            text="対局開始の5〜10分前に新規棋譜を自動取得"
                 f" (毎時 {minutes} から数分ずらして実行)",
            variable=self.auto_update_var,
            command=self._on_auto_update_toggle).pack(anchor=tk.W)
        row = ttk.Frame(fg_frame)
        row.pack(anchor=tk.W, pady=(6, 0))
        ttk.Button(row, text="今すぐ更新",
                   command=lambda: self._enqueue_floodgate("手動")).pack(
            side=tk.LEFT)
        if not hasattr(self, "_rescan_days_var"):
            self._rescan_days_var = tk.IntVar(value=7)
        ttk.Label(row, text="  過去").pack(side=tk.LEFT)
        ttk.Spinbox(row, from_=1, to=60, width=4,
                    textvariable=self._rescan_days_var).pack(side=tk.LEFT)
        ttk.Button(row, text="日分を再走査 (抜け確認)",
                   command=lambda: self._enqueue_floodgate(
                       "再走査", days=max(self._rescan_days_var.get(), 1))
                   ).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(fg_frame,
                  text="取り込み済み・不成立のファイルは削除されます。未終局は保持して"
                       "次サイクルで再確認、解析エラーのファイルは data/floodgate/ に"
                       "残ります。自動更新はこのウィンドウを閉じても動き続けます。",
                  foreground="gray", wraplength=620, justify=tk.LEFT).pack(
            anchor=tk.W, pady=(6, 0))

        # --- .sfen 連続対局 (専用DBへ。他の取り込み先とは混ざらない) ---
        sfen_frame = ttk.LabelFrame(win, text=".sfen 連続対局 (.sfen DBへ)",
                                    padding=8)
        sfen_frame.pack(fill=tk.BOTH, expand=False, padx=8, pady=4)
        sfen_buttons = ttk.Frame(sfen_frame)
        sfen_buttons.pack(fill=tk.X)
        ttk.Button(sfen_buttons, text=".sfenファイルを追加...",
                   command=self._add_sfen_files).pack(side=tk.LEFT)
        ttk.Button(sfen_buttons, text="追記を取り込み (全バッチ)",
                   command=self._refresh_sfen_batches_ingest).pack(
            side=tk.LEFT, padx=(6, 0))
        ttk.Button(sfen_buttons, text="選択バッチを削除",
                   command=self._delete_selected_batch).pack(
            side=tk.LEFT, padx=(6, 0))
        ttk.Label(sfen_frame, foreground="gray", wraplength=620,
                  justify=tk.LEFT,
                  text="1ファイル=1バッチ。課題局面までの共通手順は「(共通)」名の"
                       "擬似対局として1回だけ登録される (終局理由列は「課題局面」)。"
                       "追記は自動で追加分のみ、既読部分が書き換わったファイルは "
                       "conflict になる (削除→追加で編集を反映)。").pack(
            anchor=tk.W, pady=(4, 0))
        tree_holder = ttk.Frame(sfen_frame)
        tree_holder.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        cols = ("batch", "label", "date", "games", "prefix", "status")
        tree = ttk.Treeview(tree_holder, columns=cols, show="headings",
                            height=5)
        for col, text_, width, anchor in (
                ("batch", "バッチ", 190, tk.W), ("label", "ラベル", 160, tk.W),
                ("date", "日付", 90, tk.W), ("games", "局数", 60, tk.E),
                ("prefix", "課題手数", 70, tk.E),
                ("status", "状態", 70, tk.CENTER)):
            tree.heading(col, text=text_)
            tree.column(col, width=width, anchor=anchor,
                        stretch=col in ("batch", "label"))
        sfen_scroll = ttk.Scrollbar(tree_holder, orient=tk.VERTICAL,
                                    command=tree.yview)
        tree.configure(yscrollcommand=sfen_scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sfen_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._sfen_tree = tree
        self._refresh_sfen_list()

        log_frame = ttk.LabelFrame(win, text="ログ", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))
        text = tk.Text(log_frame, wrap=tk.NONE, state=tk.DISABLED,
                       font=self.mono_font, height=8)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL,
                                   command=text.yview)
        text.configure(yscrollcommand=log_scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._update_log_text = text
        if self._update_log_lines:
            text.config(state=tk.NORMAL)
            text.insert(tk.END, "\n".join(self._update_log_lines) + "\n")
            text.see(tk.END)
            text.config(state=tk.DISABLED)

    # -- .sfen 連続対局の取り込み・バッチ管理 --------------------------------

    def _sfen_db(self) -> str:
        return self.sfen_db_var.get().strip() or str(DEFAULT_SFEN_DB)

    def _refresh_sfen_list(self) -> None:
        tree = getattr(self, "_sfen_tree", None)
        if tree is None or not tree.winfo_exists():
            return
        tree.delete(*tree.get_children())
        db = self._sfen_db()
        if not Path(db).is_file():
            return
        try:
            batches = list_batches(db)
        except Exception:  # noqa: BLE001 - 対象DBが前例DB等でも落とさない
            return
        for b in batches:
            tree.insert("", tk.END, iid=b["path"], values=(
                b["stem"], b["label"], b["date"], b["games"],
                b["prefix"] or "-", b["status"]))

    def _guard_sfen_db(self) -> str | None:
        """取り込み先が実対局の前例DBだったら警告する (誤爆防止)。"""
        db = self._sfen_db()
        if Path(db).is_file():
            try:
                conn = open_read_only(db)
                try:
                    mixed = conn.execute(
                        "SELECT 1 FROM games WHERE source != 'sfen' LIMIT 1"
                    ).fetchone()
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                mixed = None
            if mixed and not messagebox.askyesno(
                    "確認",
                    "このDBには実対局の前例が入っています。.sfen連続対局は専用DBに"
                    "分けることを推奨します。\nそれでも取り込みますか？",
                    parent=self._dialog_parent()):
                return None
        return db

    def _add_sfen_files(self) -> None:
        db = self._guard_sfen_db()
        if db is None:
            return
        paths = filedialog.askopenfilenames(
            title=".sfen 連続対局ファイルを選択", parent=self._dialog_parent(),
            filetypes=[("sfen棋譜", "*.sfen"), ("All", "*")])
        batches = list_batches(db) if Path(db).is_file() else []
        for p in paths:
            # パス一致だけでなくバッチ名一致も既知扱いにする (移動したファイルは
            # ライブラリ側が台帳を付け替え、同名別内容なら拒否される)
            stem = Path(p).stem
            known = any(b["path"] == p or b["stem"] == stem for b in batches)
            if known:
                self._enqueue_sfen_ingest(p, None, None)  # 追記は尋ねず自動
            else:
                params = self._sfen_preview_dialog(p)
                if params is not None:
                    self._enqueue_sfen_ingest(p, *params)

    def _sfen_preview_dialog(self, path: str):
        """新規バッチの取り込み前プレビュー。(label, date) か None(スキップ)。"""
        try:
            scan = scan_file(path)
        except OSError as exc:
            messagebox.showerror("エラー", f"{path}: {exc}",
                                 parent=self._dialog_parent())
            return None
        dialog = tk.Toplevel(self._dialog_parent())
        dialog.title("バッチの取り込み")
        dialog.transient(self._dialog_parent())
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        rows = [("ファイル", Path(path).name),
                ("局数", f"{scan.games}局"
                         + (f" (警告 {len(scan.skipped)}行)" if scan.skipped else "")),
                ("課題局面", f"{scan.prefix_len}手目" if scan.prefix_len
                             else "検出なし (1局のみ等)")]
        for i, (k, v) in enumerate(rows):
            ttk.Label(frame, text=f"{k}:").grid(row=i, column=0, sticky=tk.W)
            ttk.Label(frame, text=v).grid(row=i, column=1, sticky=tk.W, padx=6)
        ttk.Label(frame, text="日付:").grid(row=3, column=0, sticky=tk.W)
        date_var = tk.StringVar(value=scan.date_str)
        ttk.Entry(frame, textvariable=date_var, width=24).grid(
            row=3, column=1, sticky=tk.W, padx=6)
        ttk.Label(frame, text="ラベル:").grid(row=4, column=0, sticky=tk.W)
        label_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=label_var, width=32)
        entry.grid(row=4, column=1, sticky=tk.W, padx=6)
        ttk.Label(frame, foreground="gray",
                  text="エンジン名・深さ等 (対局者名として表示。空ならファイル名)"
                  ).grid(row=5, column=1, sticky=tk.W, padx=6)
        result: dict = {}
        btns = ttk.Frame(frame)
        btns.grid(row=6, column=0, columnspan=2, pady=(10, 0))

        def accept():
            result["ok"] = True
            dialog.destroy()

        ttk.Button(btns, text="取り込む", command=accept).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(btns, text="スキップ", command=dialog.destroy).pack(
            side=tk.LEFT, padx=4)
        entry.focus_set()
        dialog.wait_window()
        if not result.get("ok"):
            return None
        return label_var.get().strip(), date_var.get().strip()

    def _enqueue_sfen_ingest(self, path: str, label, date_str) -> None:
        db = self._sfen_db()

        def job():
            ingest_file(db, path, label=label or "",
                        date_str=date_str if date_str else None,
                        log_line=lambda m: self._ulog(f"[.sfen] {m}"))
            self.root.after(0, self._refresh_sfen_list)

        self._db_jobs.put((".sfen取り込み", job))

    def _refresh_sfen_batches_ingest(self) -> None:
        """登録済みバッチを再走査し、追記分を取り込む (1ジョブに集約)。

        1ヶ月以上前の日付のバッチは対象外 (連続対局が今も伸びていることはまず
        無く、大量のファイル読みを避ける)。古いバッチを更新したいときは
        「.sfenファイルを追加...」で同じファイルを選べば日付に関係なく処理
        される。変更なしは無言、特記事項と最後の要約だけログに出す。"""
        if getattr(self, "_sfen_refresh_queued", False):
            self._ulog("[.sfen] 更新チェックは既に実行待ちです")
            return
        db = self._sfen_db()
        if not Path(db).is_file():
            return
        cutoff = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d")
        targets, old, missing = [], 0, 0
        for b in list_batches(db):
            if b["date"] and b["date"] < cutoff:
                old += 1
            elif not Path(b["path"]).is_file():
                missing += 1
            else:
                targets.append(b["path"])
        if not targets:
            self._ulog(f"[.sfen] 更新チェック対象なし"
                       f" (1ヶ月超 {old}件 / ファイル欠落 {missing}件)")
            return
        self._sfen_refresh_queued = True

        def job():
            added = conflicts = unchanged = 0
            try:
                for p in targets:
                    r = ingest_file(db, p)
                    if r.conflict or r.rebuilt or r.added:
                        self._ulog(f"[.sfen] {Path(p).stem}: {r.summary()}")
                    if r.conflict:
                        conflicts += 1
                    elif r.unchanged:
                        unchanged += 1
                    else:
                        added += r.added
                extra = (f" / 1ヶ月超スキップ {old}件" if old else "") + \
                        (f" / ファイル欠落 {missing}件" if missing else "")
                self._ulog(f"[.sfen] 更新チェック完了: 確認 {len(targets)}件 / "
                           f"追加 {added}局 / conflict {conflicts}件 / "
                           f"変更なし {unchanged}件{extra}")
            finally:
                self._sfen_refresh_queued = False
                self.root.after(0, self._refresh_sfen_list)

        self._db_jobs.put((".sfen更新チェック", job))

    def _delete_selected_batch(self) -> None:
        tree = getattr(self, "_sfen_tree", None)
        if tree is None:
            return
        selection = tree.selection()
        if not selection:
            return
        path = selection[0]
        stem = Path(path).stem
        db = self._sfen_db()
        if not messagebox.askyesno(
                "確認", f"バッチ「{stem}」を専用DBから完全に削除します。\n"
                        "よろしいですか？ (.sfenファイル自体は消えません)",
                parent=self._dialog_parent()):
            return

        def job():
            deleted = delete_batch(db, path,
                                   log_line=lambda m: self._ulog(f"[.sfen] {m}"))
            self.root.after(0, self._refresh_sfen_list)
            self._ulog(f"[.sfen] {stem}: {deleted}件削除")

        self._db_jobs.put((".sfenバッチ削除", job))

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
                     "「棋譜コピー」で将棋盤GUI用にクリップボードへコピー)")
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
        棋譜をクリップボードへコピーする (将棋盤GUI用) フォールバックを行う。"""
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

        将棋盤GUIの検討中のウィンドウに Ctrl+V (⌘V) で貼り付けられる。
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
                f"棋譜をコピーしました ({origin})。将棋盤GUIに Ctrl+V / ⌘V で貼り付けられます。")
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

    # -- 将棋盤GUIの局面追従 (sync) -----------------------------------------

    def _on_sync_toggle(self) -> None:
        self._save_config()
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
            self.auto_update_var.set(bool(config.get("floodgate_auto_update")))
            self.sync_var.set(bool(config.get("sync_follow", True)))
            # 旧設定からの移行: 対象DB未設定なら当時のビューアDBを引き継ぐ
            self.fg_db_var.set(config.get("floodgate_db_path")
                               or config.get("db_path", ""))
            if config.get("private_db_path"):
                self.private_db_var.set(config["private_db_path"])
            if config.get("sfen_db_path"):
                self.sfen_db_var.set(config["sfen_db_path"])
            if config.get("sync_file"):
                self._sync_file = Path(config["sync_file"])
        except (OSError, json.JSONDecodeError):
            pass

    def _save_config(self) -> None:
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps({
                "db_path": self.db_var.get().strip(),
                "last_sfen": self.sfen_var.get().strip(),
                "floodgate_auto_update": self.auto_update_var.get(),
                "sync_follow": self.sync_var.get(),
                "floodgate_db_path": self.fg_db_var.get().strip(),
                "private_db_path": self.private_db_var.get().strip(),
                "sfen_db_path": self.sfen_db_var.get().strip()},
                ensure_ascii=False))
        except OSError:
            pass


def main() -> None:
    setup_dpi_awareness()
    root = tk.Tk()
    PrecedentViewer(root)
    root.mainloop()


if __name__ == "__main__":
    main()
