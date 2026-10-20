#!/usr/bin/env python3
"""KiriCompass 前例ビューア: SFENを貼り付けて前例DBを検索する。

起動:  python3 tools/precedent_gui.py [db_path]

- SFEN欄には "position sfen ...", "sfen ...", "startpos", 素のSFEN の
  いずれを貼り付けてもよい。手数部分は無視される。
- 前例をダブルクリックすると棋譜ビューアをブラウザで開く (URLが無い前例は
  棋譜のクリップボードコピーにフォールバック)。
  「棋譜コピー」ボタンは棋譜をクリップボードにコピーする。将棋盤GUIの検討中
  ウィンドウに Ctrl+V (⌘V) でそのまま貼り付けられる (元ファイルが残っていれば
  その内容を、なければDBから復元した棋譜を使う)。
  「ファイルで開く」ボタンは既定アプリで開く (新規ウィンドウ)。
- 前例を選択すると、その局面での評価値と読み筋(記録があれば)を表示する。
- 「将棋盤GUIの局面に追従」(既定ON) は、USIエンジン (tools/usi_engine.py) が
  書き出す同期ファイルを追従して自動検索する。単体利用時はOFFにしてよい。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import queue
import re
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
                          compute_source_intervals, tournament_label)
from kifudb.db import open_for_write, open_read_only, resolve_db_path  # noqa: E402
from kifudb.floodgate import days_behind, update_once  # noqa: E402
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


def windows_dpi() -> int:
    """WindowsのシステムDPIを返す (取得できなければ96)。

    DPI対応を宣言したプロセスでは、Tkのスケーリングと固定ピクセル寸法を
    このDPIに合わせて自前で調整しないと、表示スケール125%/150%の環境で
    「フォントだけ拡大されて列幅・行高がそのまま → 文字が潰れる/切れる」
    が起きる。Tk任せにせず明示的に取得して決定的にする。"""
    import ctypes
    try:
        return int(ctypes.windll.user32.GetDpiForSystem())  # Win10 1607+
    except (AttributeError, OSError):
        pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return dpi or 96
    except (AttributeError, OSError):
        return 96


def pick_mono_font(root: tk.Tk) -> str:
    import tkinter.font as tkfont
    available = set(tkfont.families(root))
    for name in MONO_FONT_CANDIDATES:
        if name in available:
            return name
    return "TkFixedFont"


# Windowsの太字見出し用: 真の太字を持つ実在の日本語UIフォント。既定の
# TkDefaultFont (Segoe UI) は日本語グリフを持たず、フォントリンクの代替
# 描画に太字合成 (機械的な重ね描き) がかかって日本語の太字が潰れるため、
# 実在のフォントを明示して差し替える。
WIN_HEADING_FONT_CANDIDATES = [
    "Yu Gothic UI", "游ゴシック",     # Windows 10+
    "Meiryo UI", "メイリオ",          # Windows Vista+
    "BIZ UDPゴシック", "BIZ UDPGothic",
]

CONFIG_PATH = RUNTIME_DIR / "gui_config.json"
# 永続ログ。取り込み中のエラー (どのファイルで何が起きたか) はここに残る。
# GUIの更新ログ窓は一時表示なので、閉じた後や落ちた後の追跡用に必須。
LOG_FILE = RUNTIME_DIR / "kiricompass.log"


def setup_file_logging() -> None:
    """kifudb のログ (取り込みの逐次エラー等) を runtime/kiricompass.log に残す。

    `_ingest_file` は不良ファイルを `error: <ファイル名> (...)` として
    log.warning に出す。GUI単体ではこのロガーにハンドラが無く握り潰されて
    いたため、どのファイルで失敗したか後から分からなかった。ここでファイル
    ハンドラを付けることで、閉じた後・落ちた後でも原因ファイルを追える。"""
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger = logging.getLogger("kifudb")
        logger.setLevel(logging.INFO)
        # 二重起動でハンドラを重ねない
        if not any(isinstance(h, logging.handlers.RotatingFileHandler)
                   and getattr(h, "baseFilename", "") == str(LOG_FILE)
                   for h in logger.handlers):
            logger.addHandler(handler)
    except OSError:
        pass  # ログが書けなくても本体は動かす
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
# 公開前例DB (floodgate更新・公開棋譜取り込みの対象) の既定パス。配布された
# サンプルDBを使う場合は「DB更新...」の「参照...」で選び直す。無ければ確認の
# 上で空のDBを新規作成する (_ensure_db) ので、CLIなしでゼロから始められる。
DEFAULT_PUBLIC_DB = Path(__file__).resolve().parent.parent / "data" / "csa.db"
# 起動時チェックで自動的に遡る日数の上限。DB内の最新floodgate対局からの
# 空きがこれを超える場合は取りに行かず、手動の再走査を案内する — 大きい
# 取得ほど明示的なユーザー操作で行う (常駐ポーリングではなく「ユーザーの
# 操作に応えて取りに行く」道具に留めるための方針)。
STARTUP_CHECK_MAX_DAYS = 7
# 島表 (出典→game_id区間) の永続キャッシュ。DBが変わらない限り起動時の
# 再計算 (gamesテーブル全走査) を丸ごと省ける。
INTERVALS_CACHE_PATH = RUNTIME_DIR / "source_intervals.json"
# キャッシュ形式版。島の分類規則が変わったら上げる (旧キャッシュを無効化して
# 再計算させる)。v2 = 電竜戦TSECの細分を導入。
INTERVALS_CACHE_VERSION = 2


def norm_db_path(raw: str) -> str:
    """入力欄・設定・引数のDBパスを正規化した文字列にする (空は空のまま)。

    素の名前 ('csa.db') は data/ に解決する (kifudb.db.resolve_db_path)。
    GUI内の全読み取り箇所がこれを通ることで、キャッシュのキー・Readerの
    同一性判定・存在チェックがCLI/エンジンと同じDBを指すことを保証する。"""
    raw = raw.strip()
    return str(resolve_db_path(raw)) if raw else ""


def load_interval_sidecar(db_path: str):
    """前回起動時に計算した島表を、検証付きで読み込む。

    DBの MAX(game_id) と総対局数が保存時と一致する場合だけ採用する
    (増分取り込みや再構築があれば不一致になり、呼び出し側が再計算する)。
    戻り値: (intervals, max_gid, count) または None。"""
    try:
        data = json.loads(INTERVALS_CACHE_PATH.read_text(encoding="utf-8"))
        entry = data[str(resolve_db_path(db_path).resolve())]
        if entry.get("v") != INTERVALS_CACHE_VERSION:
            return None  # 分類規則が古い形式のキャッシュは使わない
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
        data[str(resolve_db_path(db_path).resolve())] = {
            "v": INTERVALS_CACHE_VERSION,
            "max_gid": max_gid, "count": count,
            "intervals": [list(iv) for iv in intervals]}
        INTERVALS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        INTERVALS_CACHE_PATH.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 保存失敗は次回再計算するだけ
RESULT_JA = {1: "先手勝", 2: "後手勝", 0: "引分", None: "―"}
WINNER_JA = {1: "先", 2: "後"}  # それ以外 (引分・結果なし) は "-"
SFEN_STATUS_JA = {"ok": "OK", "conflict": "競合"}

_DATE_INPUT_RE = re.compile(
    r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"
    r"(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$")


def normalize_date_input(text: str) -> str | None:
    """日付入力を 'YYYY-MM-DD HH:MM:SS' に揃える。空欄は ''、不正は None。

    時刻を省いた '2026-10-20' もそのまま保存すると並べ替えキーが作れず
    「日付不明」扱い (前例一覧の最後尾) になるため、00:00:00 を補う。"""
    text = text.strip()
    if not text:
        return ""
    m = _DATE_INPUT_RE.match(text)
    if not m:
        return None
    y, mo, d, h, mi, sec = m.groups()
    try:
        value = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0),
                         int(sec or 0))
    except ValueError:
        return None
    return value.strftime("%Y-%m-%d %H:%M:%S")


# 「Xでポスト」は隠し機能: 起動時に runtime/post_template.txt が存在する
# 場合にのみボタンが現れる (作者の個人用途。有効化したい人だけが自分で
# ファイルを置く。空ファイルなら下のデフォルト文面が使われ、内容を書けば
# それがテンプレートになり再起動なしで反映される)。
# 使える変数: {tournament} {black} {white} {date} {ply} {next_move}
#             {result} {reason} {ply_count} {source} {event} {url}
POST_TEMPLATE_FILE = RUNTIME_DIR / "post_template.txt"
DEFAULT_POST_TEMPLATE = """{tournament}

{date}　{black} - {white}
{ply}手目 {next_move}

{url}"""


def load_post_template() -> str:
    try:
        text = POST_TEMPLATE_FILE.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_POST_TEMPLATE
    return text if text.strip() else DEFAULT_POST_TEMPLATE


class PrecedentViewer:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("KiriCompass")
        # Windows: 表示スケールに合わせてTkのポイント→ピクセル換算と、
        # このGUIの固定ピクセル寸法 (_px) を明示的に揃える。Tkの自動判定に
        # 任せると、DPI宣言との組み合わせ次第でフォントと行高・列幅がずれて
        # 文字が潰れる環境がある (Windows 125%/150%スケールで報告あり)。
        if sys.platform == "win32":
            dpi = windows_dpi()
            root.tk.call("tk", "scaling", dpi / 72.0)
            self._ui_scale = dpi / 96.0
        else:
            self._ui_scale = 1.0
        root.geometry(f"{self._px(900)}x{self._px(810)}")

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
        self._db_job_progress = ""       # 実行中ジョブの進捗 (ステータス表示用)
        self._floodgate_queued = False   # floodgateジョブの重複投入防止
        # sfen混入ガードの結果キャッシュ: path -> (mtime_ns, has_sfen)。
        # 該当なしのDBでは判定がgames全走査になるため、毎回は走らせない。
        self._sfen_check_cache: dict[str, tuple[int, bool]] = {}
        self._update_win: tk.Toplevel | None = None
        self._update_log_lines: list[str] = []  # ウィンドウ再表示用の履歴
        # 更新ログはワーカーから大量に来ることがあるため、まとめて画面へ流す
        self._log_pending: list[str] = []
        self._log_lock = threading.Lock()
        self._log_flush_scheduled = False
        self._sfen_list_token = 0        # バッチ一覧の非同期読み込みの世代
        self._sfen_preparing = False     # .sfen追加の事前確認 (読み込み) 中
        self._reselect_game_id: int | None = None  # 再表示後に選択し直す前例
        self._sfen_view_hint_shown = False
        self._name_db_query: str | None = None  # DB側に適用中の名前フィルタ

        self._setup_fonts()
        self._build_widgets()
        self._load_config()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        if len(sys.argv) > 1:
            self.db_var.set(norm_db_path(sys.argv[1]))
        self._prime_source_intervals()  # 起動直後から先読みを始める
        threading.Thread(target=self._db_worker, daemon=True).start()
        self._poll_sync_file()
        # 追従ONなら起動直後に現在局面を表示するところまで行う
        self.root.after(200, self._initial_sync_search)
        # 起動時の新規棋譜チェック (ウィンドウ表示を待ってから)
        self.root.after(1200, self._startup_floodgate_check)

    def _px(self, n: int) -> int:
        """設計時 (96dpi) のピクセル値を現在の表示スケールに換算する。"""
        return round(n * self._ui_scale)

    def _setup_fonts(self) -> None:
        import tkinter.font as tkfont
        ui_family = None
        if sys.platform == "win32":
            # 既定の TkDefaultFont (Segoe UI 9pt) は日本語グリフを持たず、
            # フォントリンクの細い代替描画になって見劣りする。UI全体を
            # 実在の日本語UIフォント10ptに統一する (太字も真のウェイトが
            # 使えるようになり、見出しの潰れも起きない)。
            available = set(tkfont.families(self.root))
            ui_family = next((n for n in WIN_HEADING_FONT_CANDIDATES
                              if n in available), None)
            if ui_family:
                for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                             "TkHeadingFont", "TkCaptionFont",
                             "TkTooltipFont", "TkIconFont"):
                    try:
                        tkfont.nametofont(name).configure(
                            family=ui_family, size=10)
                    except tk.TclError:
                        pass
        family = pick_mono_font(self.root)
        size = 13 if sys.platform == "darwin" else 11
        self.mono_font = tkfont.Font(family=family, size=size)
        style = ttk.Style(self.root)
        # 行高はフォントの実測 (ピクセル、スケール済み) から取るので、
        # 余白ぶんだけを _px で換算する。ここが食い違うと文字が縦に潰れる。
        row_height = self.mono_font.metrics("linespace") + self._px(8)
        # 表本体は等幅 (数字の桁揃えのため)。見出し行は「先手・後手・
        # 終局理由」等の言葉なので等幅にする意味がなく、既定のUIフォント
        # そのまま (太字・サイズ調整もしない — 作者判断)。
        style.configure("Treeview", font=self.mono_font, rowheight=row_height)
        style.configure("Treeview.Heading",
                        font=tkfont.nametofont("TkDefaultFont"))

    # -- layout --------------------------------------------------------

    def _build_widgets(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="DB:").grid(row=0, column=0, sticky=tk.W)
        self.db_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.db_var).grid(
            row=0, column=1, sticky=tk.EW, padx=4)
        ttk.Button(top, text="参照...", command=self._browse_db).grid(row=0, column=2)
        self.startup_check_var = tk.BooleanVar(value=True)
        self.sfen_db_var = tk.StringVar(value=str(DEFAULT_SFEN_DB))
        # floodgate更新・公開棋譜の取り込み先DB。ビューアの表示DBに追従させると
        # .sfen DB等を開いている間の自動サイクルが誤爆するため、固定で持つ。
        self.fg_db_var = tk.StringVar(value=str(DEFAULT_PUBLIC_DB))
        self.private_db_var = tk.StringVar(value=str(DEFAULT_PRIVATE_DB))

        ttk.Label(top, text="SFEN:").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        self.sfen_var = tk.StringVar()
        # DB欄と同じ既定フォントに揃える (SFEN欄だけ等幅だと浮いて見える)
        sfen_entry = ttk.Entry(top, textvariable=self.sfen_var)
        sfen_entry.grid(row=1, column=1, sticky=tk.EW, padx=4, pady=(6, 0))
        sfen_entry.bind("<Return>", lambda _e: self.search())
        self.search_button = ttk.Button(top, text="検索", command=self.search)
        self.search_button.grid(row=1, column=2, pady=(6, 0))

        # 追従チェックは検索ボタンの右
        self.sync_var = tk.BooleanVar(value=True)  # 既定で追従ON
        sync_check = ttk.Checkbutton(
            top, text="将棋盤GUIの局面に追従",
            variable=self.sync_var, command=self._on_sync_toggle)
        sync_check.grid(row=1, column=3, sticky=tk.W, padx=(6, 0), pady=(6, 0))
        top.columnconfigure(1, weight=1)

        # DB行・SFEN行の収納 (トグルボタンは候補手右の切替ボタン群の上)。
        # フレームごと外すので、畳んだときは余白も含めて完全に詰まる。
        self._top_frame = top
        self._top_collapsed = False

        panes = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._panes = panes  # 収納トグルが top フレームを戻す位置の基準

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
            self.cand_tv.column(col, width=self._px(width), anchor=anchor,
                                stretch=False)
        # 列幅の合計にウィジェット自体を合わせ、左に詰める
        self.cand_tv.pack(side=tk.LEFT, fill=tk.Y)
        cand_scroll = ttk.Scrollbar(cand_frame, orient=tk.VERTICAL,
                                    command=self.cand_tv.yview)
        self.cand_tv.configure(yscrollcommand=cand_scroll.set)
        cand_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        # 候補手の右: 3系統DB切替。DB行/SFEN行の収納トグルはウィンドウ右端。
        switch = ttk.Frame(cand_holder)
        switch.pack(side=tk.LEFT, anchor=tk.N, padx=(10, 0), pady=4)
        ttk.Button(switch, text="DB更新...", width=9,
                   command=self._open_update_window).pack(fill=tk.X)
        self._db_switch_buttons: dict[str, ttk.Button] = {}
        for i, (text_, kind) in enumerate((("Public", "public"),
                                           ("Private", "private"),
                                           ((".sfen"), "sfen"))):
            btn = ttk.Button(switch, text=text_, width=9,
                             command=lambda k=kind: self._switch_db(k))
            btn.pack(fill=tk.X, pady=(12, 1) if i == 0 else 1)
            self._db_switch_buttons[kind] = btn
        # 選択中のDBをボタンの ● 印で示す (パス編集にも追従)
        for var in (self.db_var, self.fg_db_var, self.private_db_var,
                    self.sfen_db_var):
            var.trace_add("write",
                          lambda *_args: self._update_db_switch_marks())
        self._update_db_switch_marks()
        self._collapse_btn = ttk.Button(cand_holder, text="▲", width=2,
                                        command=self._toggle_top_rows)
        self._collapse_btn.pack(side=tk.RIGHT, anchor=tk.N, pady=4)
        panes.add(cand_holder, weight=1)

        # 枠見出しを「前例   [検索式] [対局者名検索]」にする (labelwidget)。
        # 入力は取得に影響せず、ボタン (またはEnter) で初めて適用される。
        prec_header = ttk.Frame(panes)
        ttk.Label(prec_header, text="前例").pack(side=tk.LEFT)
        self.name_filter_var = tk.StringVar()
        name_entry = ttk.Entry(prec_header, textvariable=self.name_filter_var,
                               width=22)
        name_entry.pack(side=tk.LEFT, padx=(16, 0))
        name_entry.bind("<Return>", lambda _e: self._search_names_in_db())
        ttk.Button(prec_header, text="対局者名検索",
                   command=self._search_names_in_db).pack(
            side=tk.LEFT, padx=(4, 0))
        # 名前フィルタの適用状態。局面切替で自動解除されても検索式は
        # テキスト欄に残るため、適用中かどうかをここで明示する。
        self.name_applied_var = tk.StringVar(value="")
        ttk.Label(prec_header, textvariable=self.name_applied_var,
                  foreground="#1a7f37").pack(side=tk.LEFT, padx=(6, 0))
        prec_frame = ttk.LabelFrame(panes, labelwidget=prec_header)

        # 出典フィルタ。切り替えるとDBから絞り込み条件付きで1ページ取り直す
        # (ロード済み分の表示切替ではない)。No. は現在の条件での新しい順の順位。
        filter_row = ttk.Frame(prec_frame)
        filter_row.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(2, 0))
        ttk.Label(filter_row, text="出典:").pack(side=tk.LEFT)
        self.source_filter_vars: dict[str, tk.BooleanVar] = {}
        for key, label in (("floodgate", "floodgate"), ("wcsc", "WCSC"),
                           ("denryusen", "電竜戦 (TSEC以外)"),
                           ("denryusen_tsec", "電竜戦 (TSEC)"),
                           ("other", "その他")):
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
            self.prec_tv.column(col, width=self._px(width), anchor=anchor,
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
        # 隠し機能: テンプレートファイルを置いた人にだけボタンを出す
        self.post_x_button = None
        if POST_TEMPLATE_FILE.is_file():
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
        self.status_var = tk.StringVar(value="DBとSFENを指定して検索してください。")
        # 右のボタン類を先に確保し、ステータスは残り幅に収めて切り詰める
        # (長いメッセージでもボタンを画面外へ押し出さないようにする)。
        self.more_button = ttk.Button(bottom, text=f"さらに{PAGE_SIZE}件表示",
                                      command=self._load_more, state=tk.DISABLED)
        self.more_button.pack(side=tk.RIGHT, padx=6)
        # DB更新の稼働表示 (更新ジョブ実行中に控えめに出す)
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

    def search(self, quiet: bool = False) -> None:
        """検索を開始する。quiet=True は追従など自動起点の検索で、
        入力エラーをダイアログでなくステータスバーに出す (盤面GUIの操作の
        たびにモーダルが出るのを防ぐ)。"""
        if self._search_running:
            return
        db_path = norm_db_path(self.db_var.get())
        sfen = self.sfen_var.get().strip()
        if not db_path or not Path(db_path).is_file():
            message = "有効なDBファイルを指定してください。"
            if quiet:
                self.status_var.set(f"追従を保留中: {message}")
            else:
                messagebox.showerror("エラー", message)
            return
        if not sfen:
            if not quiet:
                messagebox.showerror("エラー", "SFENを貼り付けてください。")
            return
        self._save_config()
        self._prime_source_intervals()  # DBが変わっていたら先読みを開始
        self._search_running = True
        self.search_button.config(state=tk.DISABLED)
        self.status_var.set("検索中...")
        # フィルタ状態は tk 変数なのでメインスレッドで読み取って渡す
        # 局面の切り替えでは対局者名フィルタを解除する (レアな名前のまま
        # 全走査が走ると切替のたびに重い)。検索式はテキスト欄に残るので、
        # 必要ならボタンで再適用する。
        self._name_db_query = None
        self.name_applied_var.set("")
        threading.Thread(target=self._search_task,
                         args=(db_path, sfen, self._enabled_sources(), quiet),
                         daemon=True).start()

    def _search_task(self, db_path: str, sfen: str, sources,
                     quiet: bool = False) -> None:
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
            self.root.after(0, self._show_error, str(exc), quiet)

    def _show_error(self, message: str, quiet: bool = False) -> None:
        self._search_running = False
        self._reselect_game_id = None
        self.search_button.config(state=tk.NORMAL)
        if quiet:  # 追従など自動起点の検索はダイアログを出さない
            self.status_var.set(f"検索エラー: {message}")
            return
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
            # 先手勝率 = 先手勝 ÷ (先手勝 + 後手勝 + 引分)
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
        reselect, self._reselect_game_id = self._reselect_game_id, None
        for index, p in enumerate(self.precedents):
            if reselect is not None and p.game_id == reselect:
                self.prec_tv.selection_set(str(index))
                self.prec_tv.see(str(index))
                break
        self._refetch_if_filter_changed(sources)

    def _prime_source_intervals(self) -> None:
        """出典絞り込み用の島表をバックグラウンドで先読みする (DBごとに一度)。

        起動直後と検索開始時に呼ばれる。専用の読み取り接続で走るため、検索
        など他の処理をブロックしない。失敗した場合は記録を消し、次の機会に
        再試行する (絞り込み自体は検索スレッドの同期計算でも正しく動く)。"""
        db_path = norm_db_path(self.db_var.get())
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
            self._db_job_progress = ""
            self.root.after(0, self._set_update_status)
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - 次のジョブは続行する
                self._ulog(f"[{kind}] 失敗: {exc}")
            finally:
                self._db_job_running = None
                self._db_job_progress = ""
                self.root.after(0, self._set_update_status)

    def _set_job_progress(self, text: str) -> None:
        """実行中ジョブの進捗表示を更新する (どのスレッドからでも呼べる)。

        ログには流さない: 進捗のたびにログ行を足すと肝心の結果が埋もれる。"""
        self._db_job_progress = text
        self.root.after(0, self._set_update_status)

    def _on_close(self) -> None:
        """DB更新中の終了は確認する (中断自体は安全だが、黙って捨てない)。"""
        if self._db_job_running or not self._db_jobs.empty():
            waiting = self._db_jobs.qsize()
            if not messagebox.askyesno(
                    "確認",
                    f"DB更新中です ({self._db_job_running or '待機中'}"
                    + (f"、ほか待機 {waiting}件" if waiting else "") + ")。\n"
                    "今終了すると実行中の取り込みは中断され、その分はDBに"
                    "反映されません (DBが壊れることはありません)。\n\n"
                    "終了しますか？", parent=self._dialog_parent()):
                return
        self.root.destroy()

    def _update_db_switch_marks(self) -> None:
        """ビューアが開いているDBに対応する切替ボタンへ ● を付ける。"""
        def norm(path: str) -> str:
            path = path.strip()
            try:
                return str(resolve_db_path(path).resolve()) if path else ""
            except OSError:
                return path
        current = norm(self.db_var.get())
        for kind, (label, var) in (("public", ("Public", self.fg_db_var)),
                                   ("private", ("Private", self.private_db_var)),
                                   ("sfen", ((".sfen"), self.sfen_db_var))):
            active = current and norm(var.get()) == current
            self._db_switch_buttons[kind].config(
                text=("● " if active else "") + label)

    def _initial_sync_search(self) -> None:
        """起動直後: 追従ONなら現在のsync局面を即座に検索・表示する。

        通常のポーリングは「変化」しか反応しないため、前回終了時と同じ局面が
        syncファイルに残っていると起動しても何も表示されない。ここで一度だけ
        明示的に適用する (syncファイルが無ければ前回のSFENで表示)。"""
        if not self.sync_var.get() or not self.db_var.get().strip():
            return
        try:
            data = json.loads(self._sync_file.read_text(encoding="utf-8"))
            sfen = data["sfen"]
            self._sync_mtime = self._sync_file.stat().st_mtime
        except (OSError, json.JSONDecodeError, KeyError):
            sfen = self.sfen_var.get().strip()  # syncが無ければ前回のSFEN
        if sfen and not self._search_running:
            self.sfen_var.set(sfen)
            self.search(quiet=True)

    def _toggle_top_rows(self) -> None:
        """DB行・SFEN行を収納/展開する (盤面GUI追従で使う際の省スペース化)。

        フレームごと pack から外すので、余白も含めて完全に詰まる。展開は
        panes の前に戻すことで元の位置に復帰する。"""
        self._top_collapsed = not self._top_collapsed
        if self._top_collapsed:
            self._top_frame.pack_forget()
        else:
            self._top_frame.pack(fill=tk.X, before=self._panes)
        self._collapse_btn.config(text="▼" if self._top_collapsed else "▲")

    def _switch_db(self, kind: str) -> None:
        """Public / Private / .sfen のDBをワンクリックで切り替える。

        切り替え時は出典フィルタを全てONに戻し、現在のsfenで表示を更新する。
        切り替え先のDBファイルが無ければ新規作成を提案する。"""
        var, role = {"public": (self.fg_db_var, "公開前例DB"),
                     "private": (self.private_db_var, "プライベートDB"),
                     "sfen": (self.sfen_db_var, ".sfen DB")}[kind]
        db_path = norm_db_path(var.get())
        if not db_path:
            messagebox.showerror(
                "エラー", "切り替え先のDBが未設定です。"
                          "「DB更新...」ウィンドウでパスを設定してください。",
                parent=self.root)
            return
        if not self._ensure_db(db_path, role):
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

    def _ensure_db(self, db_path: str, role: str) -> bool:
        """DBファイルが無ければ、確認の上で空のDBを新規作成する。

        配布されたサンプルDBを「参照...」で選ぶ使い方と、ゼロから作って
        取り込みを始める使い方の両方を想定する。戻り値: 使える状態なら True。"""
        if Path(db_path).is_file():
            return True
        if not messagebox.askyesno(
                "確認", f"{role}がまだありません:\n{db_path}\n\n"
                        "空のDBを新規作成しますか？\n"
                        "(既存のDBを使う場合は「いいえ」を選び、"
                        "「参照...」でファイルを指定してください)",
                parent=self._dialog_parent()):
            return False
        try:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            open_for_write(db_path).close()
        except Exception as exc:  # noqa: BLE001 - パス不正・権限などをそのまま提示
            messagebox.showerror("エラー", f"DBを作成できませんでした: {exc}",
                                 parent=self._dialog_parent())
            return False
        self._ulog(f"[DB] 新規作成: {db_path}")
        return True

    def _db_has_sfen_games(self, db_path: str) -> bool:
        """.sfen連続対局 (source='sfen') が入っているか。

        source に索引が無いため、該当なしのDBでは games 全走査になり
        大規模DBでは数秒かかる。必ずワーカースレッドから呼ぶこと
        (_sfen_guard 経由)。結果はDBファイルのmtimeが変わるまでキャッシュ
        する (書き込みは取り込み後のWALチェックポイントでmtimeに現れる)。"""
        try:
            mtime = Path(db_path).stat().st_mtime_ns
            cached = self._sfen_check_cache.get(db_path)
            if cached is not None and cached[0] == mtime:
                return cached[1]
            conn = open_read_only(db_path)
            try:
                found = conn.execute("SELECT 1 FROM games WHERE source='sfen' "
                                     "LIMIT 1").fetchone() is not None
            finally:
                conn.close()
            self._sfen_check_cache[db_path] = (mtime, found)
            return found
        except Exception:  # noqa: BLE001
            return False

    def _sfen_guard(self, db_path: str, role: str, quiet: bool = False) -> bool:
        """取り込みジョブ用のsfen混入ガード (ワーカースレッドで呼ぶ)。

        戻り値: 取り込んでよければ True。混入していれば通知して False —
        quiet はログのみ、手動操作はダイアログも出す (メインスレッド経由)。"""
        if not self._db_has_sfen_games(db_path):
            return True
        message = (f"{role}に.sfen連続対局が入っています。"
                   "実対局の取り込み先には使えません。")
        self._ulog(f"[ガード] {message} ({db_path})")
        if not quiet:
            self.root.after(0, lambda: messagebox.showerror(
                "エラー", message, parent=self._dialog_parent()))
        return False

    def _floodgate_target_db(self, quiet: bool = False) -> str | None:
        """floodgate更新の対象DB (固定設定)。無効なら通知して None。

        手動操作 (quiet=False) でDBファイルが無ければ新規作成を提案する。
        起動時チェック等 (quiet=True) はダイアログを出せないため
        ログに残してスキップ。"""
        db_path = norm_db_path(self.fg_db_var.get())
        if not db_path:
            message = "公開前例DBが未指定です"
            if quiet:
                self._ulog(f"[floodgate] {message} のためスキップ")
            else:
                messagebox.showerror(
                    "エラー", f"{message}。DB更新ウィンドウで指定してください。",
                    parent=self._dialog_parent())
            return None
        if not Path(db_path).is_file():
            if quiet:
                self._ulog(f"[floodgate] 公開前例DBが存在しません"
                           f" ({db_path}) のためスキップ")
                return None
            if not self._ensure_db(db_path, "公開前例DB"):
                return None
        # sfen混入ガードはここでは行わない: 大規模DBではgames全走査になり
        # UIが固まるため、各ジョブの先頭 (_sfen_guard, ワーカースレッド) で行う。
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
            if not self._sfen_guard(db_path, "公開前例DB", quiet=quiet):
                return
            self._ulog(f"[floodgate] 更新開始 ({label}, 過去{days}日分を照合)")
            result = update_once(db_path, FLOODGATE_MIRROR, days=days,
                                 log_line=lambda m: self._ulog(f"[floodgate] {m}"))
            self._after_db_update(
                db_path, changed=bool(result.ingest and result.ingest.added))

        self._db_jobs.put(("floodgate", job))

    def _enqueue_folder_ingest(self) -> None:
        """フォルダ内の棋譜を公開/プライベートに自動振り分けて取り込む。

        振り分けはファイル名 (stem) の出典判定: wdoor/WCSC/電竜戦と判定できる
        ものは公開前例DBへ、それ以外はプライベートDBへ。公開DBを配布しても
        私的な棋譜が混ざらないようにするための既定動作。"""
        public_db = self._floodgate_target_db()  # パス検証 (sfen混入はジョブ側)
        if public_db is None:
            return
        private_db = norm_db_path(self.private_db_var.get()) or str(DEFAULT_PRIVATE_DB)
        folder = filedialog.askdirectory(
            title="取り込む棋譜フォルダを選択", parent=self._dialog_parent())
        if not folder:
            return

        def is_public(p: Path) -> bool:
            return detect_source(p.stem) != "other"

        def job():
            if not self._sfen_guard(public_db, "公開前例DB"):
                return
            if Path(private_db).is_file() and \
                    not self._sfen_guard(private_db, "プライベートDB"):
                return
            self._ulog(f"[取り込み] 開始: {folder}")
            self._set_job_progress("(ファイルを列挙中)")
            files = [p for p in Path(folder).rglob("*")
                     if p.suffix.lower() in KIFU_SUFFIXES and p.is_file()]
            n_public = sum(1 for p in files if is_public(p))
            n_private = len(files) - n_public
            if not files:
                self._ulog("[取り込み] 対象ファイルがありません "
                           f"(対応形式: {' '.join(sorted(KIFU_SUFFIXES))})")
                return
            self._ulog(f"[取り込み] 振り分け: 公開 {n_public} / "
                       f"プライベート {n_private}")
            for count, db, role, keep in (
                    (n_public, public_db, "公開DB", True),
                    (n_private, private_db, "プライベートDB", False)):
                if not count:
                    continue
                stats = ingest_folder(
                    db, folder,
                    file_filter=lambda p, keep=keep: is_public(p) == keep,
                    progress=lambda i, n, role=role: self._set_job_progress(
                        f"({role} {i}/{n}ファイル)"))
                self._ulog(f"[取り込み {role}] {stats.summary_ja()}")
                for item in stats.error_files[:10]:
                    self._ulog(f"[取り込み {role}] エラー: {item}")
                if stats.errors:
                    more = stats.errors - min(len(stats.error_files), 10)
                    self._ulog(f"[取り込み {role}] "
                               + (f"ほか {more}件。" if more > 0 else "")
                               + "読めなかったファイルは飛ばして続行しました"
                                 f" (詳細: {LOG_FILE})")
                self._after_db_update(db, changed=stats.added > 0)

        self._db_jobs.put(("フォルダ取り込み", job))

    def _after_db_update(self, db_path: str, changed: bool = True) -> None:
        """取り込み後の島表の差分更新 (workerスレッドで実行)。

        追加分 (旧max_gid以降) だけ走査して島表にマージするので、逐次更新の
        たびに全走査へ戻らない。先読み前なら何もしない (次の絞り込みで計算)。
        changed なら、そのDBを表示中のビューアも再検索して結果を反映する。"""
        if changed:
            self.root.after(0, self._refresh_view_after_update, db_path)
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

    def _refresh_view_after_update(self, db_path: str) -> None:
        """表示中のDBが更新されたら、現在の局面を再検索して反映する。

        取り込み後も表示が古いままだと「入っていない」と誤解されるため。
        選択中の前例は再表示後も選び直す。対局者名フィルタの適用中は
        (再検索で解除されてしまうので) 自動では行わず案内だけ出す。"""
        if (norm_db_path(self.db_var.get()) != norm_db_path(db_path)
                or self.query_position is None):
            return
        if self._name_db_query:
            self.status_var.set("DBが更新されました。「検索」で最新の内容を表示します。")
            return
        if self._search_running:
            self.root.after(500, self._refresh_view_after_update, db_path)
            return
        selected = self._selected_precedent()
        self._reselect_game_id = selected.game_id if selected else None
        self.search(quiet=True)

    # -- 起動時の新規棋譜チェック --------------------------------------------

    def _startup_floodgate_check(self) -> None:
        """起動時に1回だけ、公開前例DBの空きぶんの新規棋譜を取りに行く。

        遡り日数はDB内の最新floodgate対局から逆算する (固定日数だと
        「久しぶりの起動で取りこぼす / 毎回無駄に遡る」の両方が起きる)。
        空きが STARTUP_CHECK_MAX_DAYS を超える場合は自動では取得せず、
        手動の再走査を案内する — 大きい取得ほど明示的な操作で行う方針。"""
        if not self.startup_check_var.get():
            return
        db_path = self._floodgate_target_db(quiet=True)
        if db_path is None:
            return

        def decide():  # MAX(started_at) の走査があるためワーカーで
            gap = days_behind(db_path)
            if gap is None:
                days = 2   # floodgate棋譜のない新しいDB: 今日から収集を始める
            elif gap + 1 > STARTUP_CHECK_MAX_DAYS:
                self._ulog(f"[floodgate] 最終取り込みから{gap}日空いています。"
                           f"起動時チェックの対象外のため、「DB更新...」の"
                           f"再走査 ({gap + 1}日) で追いついてください")
                return
            else:
                days = max(gap + 1, 2)  # +1=日付跨ぎの余裕
            self.root.after(0, lambda: self._enqueue_floodgate(
                "起動時", days=days, quiet=True))

        threading.Thread(target=decide, daemon=True).start()

    def _on_startup_check_toggle(self) -> None:
        self._save_config()

    def _rescan_floodgate(self) -> None:
        """再走査ボタン: 大きい日数は確認を挟む (気軽な長期walk防止)。

        取り込み済みの棋譜は台帳で判定され再ダウンロードされないが、
        日数ぶんの日別インデックス照合と、抜けがあった場合のダウンロードが
        発生する。wdoorへの気軽な大量アクセスを防ぐため15日以上は確認する。"""
        days = max(self._rescan_days_var.get(), 1)
        if days >= 15 and not messagebox.askyesno(
                "確認",
                f"過去{days}日分の日別アーカイブをwdoorと照合します。\n"
                "取り込み済みの棋譜は再ダウンロードされませんが、日数が\n"
                "大きいほど時間とサーバへのアクセスが増えます。\n\n"
                f"{days}日分の再走査を実行しますか？",
                parent=self._dialog_parent()):
            return
        self._enqueue_floodgate("再走査", days=days)

    def _set_update_status(self) -> None:
        text = ""
        if self._db_job_running:
            text = f"DB更新中: {self._db_job_running}"
            if self._db_job_progress:
                text += f" {self._db_job_progress}"
            waiting = self._db_jobs.qsize()
            if waiting:
                text += f" (待機 {waiting}件)"
        self.update_status_var.set(text)

    def _ulog(self, message: str) -> None:
        """更新ログ1行 (どのスレッドからでも呼べる)。

        画面への反映はまとめて1回の after で行う。1行ごとに after を積むと、
        警告が大量に出たときにイベントキューが溢れて画面が固まる。"""
        line = f"{datetime.now():%H:%M:%S} {message}"
        # 窓を閉じても・落ちても残るよう、ファイルにも常に書く
        logging.getLogger("kifudb.gui").info(message)
        with self._log_lock:
            self._log_pending.append(line)
            if self._log_flush_scheduled:
                return
            self._log_flush_scheduled = True
        self.root.after(50, self._flush_log)

    def _flush_log(self) -> None:
        with self._log_lock:
            lines, self._log_pending = self._log_pending, []
            self._log_flush_scheduled = False
        if not lines:
            return
        self._update_log_lines.extend(lines)
        del self._update_log_lines[:-500]
        widget = getattr(self, "_update_log_text", None)
        if widget is not None and widget.winfo_exists():
            widget.config(state=tk.NORMAL)
            widget.insert(tk.END, "\n".join(lines) + "\n")
            # 表示も履歴と同程度に保つ (巨大化するとWindowsで描画が重くなる)
            excess = int(widget.index("end-1c").split(".")[0]) - 1000
            if excess > 0:
                widget.delete("1.0", f"{excess + 1}.0")
            widget.see(tk.END)
            widget.config(state=tk.DISABLED)

    # -- DB更新ウィンドウ ---------------------------------------------------

    def _open_update_window(self) -> None:
        if self._update_win is not None and self._update_win.winfo_exists():
            self._update_win.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("DB更新")
        win.geometry(f"{self._px(700)}x{self._px(880)}")
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
        ttk.Label(db_frame, foreground="gray", wraplength=self._px(630), justify=tk.LEFT,
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
                  foreground="gray", wraplength=self._px(630), justify=tk.LEFT).pack(
            anchor=tk.W, pady=(4, 0))

        fg_frame = ttk.LabelFrame(win, text="floodgate 更新 (公開前例DBへ)",
                                  padding=8)
        fg_frame.pack(fill=tk.X, padx=8, pady=4)
        ttk.Checkbutton(
            fg_frame,
            text="起動時に新規棋譜を確認する (遡り日数はDBの最新対局から自動計算、"
                 f"最大{STARTUP_CHECK_MAX_DAYS}日)",
            variable=self.startup_check_var,
            command=self._on_startup_check_toggle).pack(anchor=tk.W)
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
                   command=self._rescan_floodgate).pack(side=tk.LEFT,
                                                        padx=(4, 0))
        ttk.Label(fg_frame,
                  text="取り込み済み・不成立のファイルは削除されます。未終局は保持して"
                       "次回再確認、解析エラーのファイルは data/floodgate/ に残ります。",
                  foreground="gray", wraplength=self._px(620), justify=tk.LEFT).pack(
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
        ttk.Label(sfen_frame, foreground="gray", wraplength=self._px(620),
                  justify=tk.LEFT,
                  text="1ファイル=1バッチ。課題局面までの共通手順は「(共通)」名の"
                       "擬似対局として1回だけ登録される (終局理由列は「課題局面」)。"
                       "追記は自動で追加分のみ、既読部分が書き換わったファイルは "
                       "競合として扱われます (削除→追加で編集を反映)。").pack(
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
            tree.column(col, width=self._px(width), anchor=anchor,
                        stretch=col in ("batch", "label"))
        sfen_scroll = ttk.Scrollbar(tree_holder, orient=tk.VERTICAL,
                                    command=tree.yview)
        tree.configure(yscrollcommand=sfen_scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sfen_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._sfen_tree = tree
        # 一覧の要約・読み込み状況 (ボタン行の右端。縦の余白を使わない)
        self._sfen_list_status = tk.StringVar(value="")
        ttk.Label(sfen_buttons, textvariable=self._sfen_list_status,
                  foreground="gray").pack(side=tk.RIGHT)
        self._refresh_sfen_list()

        log_frame = ttk.LabelFrame(win, text="ログ", padding=4)
        # 最初に下端へ確保する: 画面が狭いと後から詰めた枠ほど削られるため、
        # 取り込み結果を伝えるログではなく .sfen の一覧の方が縮むようにする
        log_frame.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True,
                       padx=8, pady=(4, 8), before=db_frame)
        # 実行中ジョブと進捗 (メイン画面の右下と同じ表示)
        ttk.Label(log_frame, textvariable=self.update_status_var,
                  foreground="#1a7f37").pack(side=tk.TOP, anchor=tk.W)
        text = tk.Text(log_frame, wrap=tk.NONE, state=tk.DISABLED,
                       font=self.mono_font, height=6)
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
        return norm_db_path(self.sfen_db_var.get()) or str(DEFAULT_SFEN_DB)

    def _refresh_sfen_list(self) -> None:
        """バッチ一覧を読み直す。DBの読み取りはバックグラウンドで行う。

        大きな .sfen DB では一覧の集計に時間がかかることがあり、メイン
        スレッドで読むと窓の表示・取り込み完了のたびに画面が固まる
        (Windowsでは「応答なし」になる)。"""
        tree = getattr(self, "_sfen_tree", None)
        if tree is None or not tree.winfo_exists():
            return
        db = self._sfen_db()
        self._sfen_list_token += 1
        token = self._sfen_list_token
        if not Path(db).is_file():
            tree.delete(*tree.get_children())
            self._sfen_list_status.set(
                ".sfen DBはまだありません (最初の取り込みで作成されます)")
            return
        self._sfen_list_status.set("バッチ一覧を読み込み中...")

        def task():
            try:
                batches, error = list_batches(db), None
            except Exception as exc:  # noqa: BLE001 - 対象DBが前例DB等でも落とさない
                batches, error = [], str(exc)
            self.root.after(0, self._show_sfen_list, token, batches, error)

        threading.Thread(target=task, daemon=True).start()

    def _show_sfen_list(self, token: int, batches: list, error) -> None:
        tree = getattr(self, "_sfen_tree", None)
        if (token != self._sfen_list_token or tree is None
                or not tree.winfo_exists()):
            return  # より新しい読み込みが走っている / 窓が閉じた
        selected = tree.selection()
        tree.delete(*tree.get_children())
        for b in batches:
            tree.insert("", tk.END, iid=b["path"], values=(
                b["stem"], b["label"], b["date"], b["games"],
                b["prefix"] or "-",
                SFEN_STATUS_JA.get(b["status"], b["status"])))
        keep = [iid for iid in selected if tree.exists(iid)]
        if keep:
            tree.selection_set(keep)
        if error:
            self._sfen_list_status.set(f"バッチ一覧を読めませんでした: {error}")
        elif batches:
            conflicts = sum(1 for b in batches if b["status"] == "conflict")
            self._sfen_list_status.set(
                f"{len(batches)}バッチ / {sum(b['games'] for b in batches)}局"
                + (f" / 競合 {conflicts}件" if conflicts else ""))
        else:
            self._sfen_list_status.set("登録済みのバッチはありません")

    def _add_sfen_files(self) -> None:
        """選んだ .sfen を取り込む。新規バッチはプレビューで確認してから。

        ファイルの読み込み・DBの確認 (混入チェック・既知バッチの照合) は
        バックグラウンドで済ませ、終わってから確認ダイアログを出す。大きな
        ファイルやDBでもボタンを押した直後に画面が固まらないようにする。"""
        if self._sfen_preparing:
            self._ulog("[.sfen] 前に選んだファイルを確認中です。少しお待ちください")
            return
        paths = filedialog.askopenfilenames(
            title=".sfen 連続対局ファイルを選択", parent=self._dialog_parent(),
            filetypes=[("sfen棋譜", "*.sfen"), ("All", "*")])
        if not paths:
            return
        db = self._sfen_db()
        self._sfen_preparing = True
        status = getattr(self, "_sfen_list_status", None)

        def show(text: str) -> None:
            if status is not None:
                self.root.after(0, status.set, text)

        def task():
            mixed, scans, error = False, {}, None
            try:
                batches = []
                if Path(db).is_file():
                    show("取り込み先DBを確認中...")
                    conn = open_read_only(db)
                    try:
                        mixed = conn.execute(
                            "SELECT 1 FROM games WHERE source != 'sfen' LIMIT 1"
                        ).fetchone() is not None
                    finally:
                        conn.close()
                    batches = list_batches(db)
                known_paths = {b["path"] for b in batches}
                known_stems = {b["stem"] for b in batches}
                for i, p in enumerate(paths, 1):
                    # パス一致だけでなくバッチ名一致も既知扱いにする (移動した
                    # ファイルはライブラリ側が台帳を付け替え、同名別内容なら拒否)
                    if str(Path(p)) in known_paths or Path(p).stem in known_stems:
                        scans[p] = None
                        continue
                    show(f"ファイルを読み込み中 ({i}/{len(paths)}): {Path(p).name}")
                    try:
                        scans[p] = scan_file(p)
                    except OSError as exc:
                        scans[p] = exc
            except Exception as exc:  # noqa: BLE001 - 理由をそのまま提示する
                error = exc
            self.root.after(0, self._continue_add_sfen, db, paths, mixed,
                            scans, error)

        threading.Thread(target=task, daemon=True).start()

    def _continue_add_sfen(self, db: str, paths, mixed: bool, scans: dict,
                           error) -> None:
        """_add_sfen_files の後半 (メインスレッド): 確認して投入する。"""
        self._sfen_preparing = False
        self._refresh_sfen_list()  # 「確認中」表示を一覧の要約に戻す
        parent = self._dialog_parent()
        if error is not None:
            messagebox.showerror("エラー", f"取り込みの準備に失敗しました: {error}",
                                 parent=parent)
            return
        if mixed and not messagebox.askyesno(
                "確認",
                "このDBには実対局の前例が入っています。.sfen連続対局は専用DBに"
                "分けることを推奨します。\nそれでも取り込みますか？",
                parent=parent):
            return
        new_paths = [p for p in paths if scans.get(p) is not None]
        auto = False   # 「残りも確認せずに取り込む」が選ばれた
        for p in paths:
            scan = scans.get(p)
            if scan is None:
                self._enqueue_sfen_ingest(p, None, None)  # 追記は尋ねず自動
                continue
            new_paths.remove(p)
            if isinstance(scan, Exception):
                self._ulog(f"[.sfen] {Path(p).name}: 読み込めません ({scan})")
                continue
            if auto:
                if scan.games:
                    self._enqueue_sfen_ingest(p, "", None)
                else:
                    self._ulog(f"[.sfen] {Path(p).name}: 取り込める対局がない"
                               "ためスキップ")
                continue
            params = self._sfen_preview_dialog(p, scan, remaining=len(new_paths))
            if params is None:
                continue
            label, date_str, auto = params
            self._enqueue_sfen_ingest(p, label, date_str)

    def _sfen_preview_dialog(self, path: str, scan, remaining: int = 0):
        """新規バッチの取り込み前プレビュー。

        戻り値: (label, date, 残りも確認せず取り込むか) または None (スキップ)。"""
        parent = self._dialog_parent()
        dialog = tk.Toplevel(parent)
        dialog.title("バッチの取り込み")
        dialog.transient(parent)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        wrap = self._px(440)
        rows = [("ファイル", Path(path).name),
                ("局数", f"{scan.games}局"
                         + (f" (読めない行 {len(scan.skipped)})" if scan.skipped else "")),
                ("課題局面", f"{scan.prefix_len}手目" if scan.prefix_len
                             else "検出なし (1局のみ・初期局面が異なる等)")]
        for i, (k, v) in enumerate(rows):
            ttk.Label(frame, text=f"{k}:").grid(row=i, column=0, sticky=tk.NW)
            ttk.Label(frame, text=v, wraplength=wrap).grid(
                row=i, column=1, sticky=tk.W, padx=6)
        row = len(rows)
        notes = []
        if scan.skipped:
            notes.append("読めない行はスキップされます。例:\n"
                         + "\n".join(scan.skipped[:3]))
        if scan.pending_tail:
            notes.append("最終行が書きかけのため、今回は取り込まず次回に回します。")
        if not scan.games:
            notes.append("取り込める対局がありません。1行に1局、"
                         "「startpos moves 7g7f ... resign」または"
                         "「sfen <局面> moves ... resign」の形式が必要です "
                         "(終局は resign / rep_draw / max_moves)。")
        if notes:
            ttk.Label(frame, text="\n\n".join(notes), wraplength=wrap,
                      justify=tk.LEFT,
                      foreground="#b35900" if scan.games else "#c62828").grid(
                row=row, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))
            row += 1
        ttk.Label(frame, text="日付:").grid(row=row, column=0, sticky=tk.W,
                                           pady=(8, 0))
        date_var = tk.StringVar(value=scan.date_str)
        date_entry = ttk.Entry(frame, textvariable=date_var, width=24)
        date_entry.grid(row=row, column=1, sticky=tk.W, padx=6, pady=(8, 0))
        ttk.Label(frame, foreground="gray",
                  text="例 2026-10-20 または 2026-10-20 13:00 (空欄なら日付なし)"
                  ).grid(row=row + 1, column=1, sticky=tk.W, padx=6)
        ttk.Label(frame, text="ラベル:").grid(row=row + 2, column=0, sticky=tk.W,
                                            pady=(4, 0))
        label_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=label_var, width=32)
        entry.grid(row=row + 2, column=1, sticky=tk.W, padx=6, pady=(4, 0))
        ttk.Label(frame, foreground="gray",
                  text="エンジン名・深さ等 (対局者名として表示。空ならファイル名)"
                  ).grid(row=row + 3, column=1, sticky=tk.W, padx=6)
        error_var = tk.StringVar()
        ttk.Label(frame, textvariable=error_var, foreground="#c62828").grid(
            row=row + 4, column=0, columnspan=2, sticky=tk.W)
        rest_var = tk.BooleanVar(value=False)
        if remaining:
            ttk.Checkbutton(
                frame, variable=rest_var,
                text=f"残り{remaining}ファイルは確認せずに取り込む "
                     "(ラベル・日付はファイル名から)").grid(
                row=row + 5, column=0, columnspan=2, sticky=tk.W, pady=(4, 0))
        result: dict = {}
        btns = ttk.Frame(frame)
        btns.grid(row=row + 6, column=0, columnspan=2, pady=(10, 0))

        def accept(_event=None):
            if not scan.games:
                return
            date_str = normalize_date_input(date_var.get())
            if date_str is None:
                error_var.set("日付の形式が正しくありません (例 2026-10-20 13:00)")
                date_entry.focus_set()
                return
            result["value"] = (label_var.get().strip(), date_str, rest_var.get())
            dialog.destroy()

        ok_button = ttk.Button(btns, text="取り込む", command=accept,
                               state=tk.NORMAL if scan.games else tk.DISABLED)
        ok_button.pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="スキップ", command=dialog.destroy).pack(
            side=tk.LEFT, padx=4)
        dialog.bind("<Return>", accept)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        entry.focus_set()

        def grab():
            try:
                dialog.grab_set()
            except tk.TclError:  # 表示前の grab は失敗する環境がある
                pass

        dialog.after(50, grab)
        dialog.wait_window()
        return result.get("value")

    def _enqueue_sfen_ingest(self, path: str, label, date_str) -> None:
        """1ファイルの取り込みを投入する。date_str=None はファイル名から。"""
        db = self._sfen_db()
        name = Path(path).name

        def job():
            self._set_job_progress(f"({name})")
            result = ingest_file(
                db, path, label=label or "", date_str=date_str,
                log_line=lambda m: self._ulog(f"[.sfen] {m}"),
                progress=lambda i, n: self._set_job_progress(
                    f"({name} {i}/{n}局)"))
            self.root.after(0, self._refresh_sfen_list)
            changed = bool(result.added or result.rebuilt)
            self._after_db_update(db, changed=changed)
            if changed and not self._sfen_view_hint_shown and \
                    norm_db_path(self.db_var.get()) != norm_db_path(db):
                self._sfen_view_hint_shown = True
                self._ulog("[.sfen] 取り込んだ対局はメイン画面の「.sfen」ボタンで"
                           "表示を切り替えると検索できます")

        self._db_jobs.put((".sfen取り込み", job))
        self._set_update_status()

    def _refresh_sfen_batches_ingest(self) -> None:
        """登録済みバッチを再走査し、追記分を取り込む (1ジョブに集約)。

        1ヶ月以上前の日付のバッチは対象外 (連続対局が今も伸びていることはまず
        無く、大量のファイル読みを避ける)。古いバッチを更新したいときは
        「.sfenファイルを追加...」で同じファイルを選べば日付に関係なく処理
        される。変更なしは無言、特記事項と最後の要約だけログに出す。
        対象の洗い出し (バッチ一覧の集計) もジョブ側で行い、画面を止めない。"""
        if getattr(self, "_sfen_refresh_queued", False):
            self._ulog("[.sfen] 更新チェックは既に実行待ちです")
            return
        db = self._sfen_db()
        if not Path(db).is_file():
            self._ulog("[.sfen] .sfen DBがまだありません。"
                       "先に「.sfenファイルを追加...」で取り込んでください")
            return
        self._sfen_refresh_queued = True

        def job():
            added = conflicts = unchanged = 0
            rebuilt = False
            try:
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
                for i, p in enumerate(targets, 1):
                    stem = Path(p).stem
                    self._set_job_progress(f"({i}/{len(targets)} {stem})")
                    r = ingest_file(db, p, progress=lambda d, n, i=i, stem=stem:
                                    self._set_job_progress(
                                        f"({i}/{len(targets)} {stem} {d}/{n}局)"))
                    if r.conflict or r.rebuilt or r.added:
                        self._ulog(f"[.sfen] {stem}: {r.summary()}")
                    rebuilt = rebuilt or r.rebuilt
                    if r.conflict:
                        conflicts += 1
                    elif r.unchanged:
                        unchanged += 1
                    else:
                        added += r.added
                extra = (f" / 1ヶ月超スキップ {old}件" if old else "") + \
                        (f" / ファイル欠落 {missing}件" if missing else "")
                self._ulog(f"[.sfen] 更新チェック完了: 確認 {len(targets)}件 / "
                           f"追加 {added}局 / 競合 {conflicts}件 / "
                           f"変更なし {unchanged}件{extra}")
                self._after_db_update(db, changed=bool(added or rebuilt))
            finally:
                self._sfen_refresh_queued = False
                self.root.after(0, self._refresh_sfen_list)

        self._db_jobs.put((".sfen更新チェック", job))
        self._set_update_status()

    def _delete_selected_batch(self) -> None:
        tree = getattr(self, "_sfen_tree", None)
        if tree is None:
            return
        selection = list(tree.selection())
        if not selection:
            messagebox.showinfo("バッチ削除", "削除するバッチを一覧で選択してください。",
                                parent=self._dialog_parent())
            return
        stems = [Path(p).stem for p in selection]
        names = "\n".join(f"・{s}" for s in stems[:10])
        if len(stems) > 10:
            names += f"\n…ほか{len(stems) - 10}件"
        db = self._sfen_db()
        if not messagebox.askyesno(
                "確認", f"次の{len(stems)}バッチを .sfen DBから完全に削除します。\n"
                        f"{names}\n\n"
                        "よろしいですか？ (.sfenファイル自体は消えません)",
                parent=self._dialog_parent()):
            return

        def job():
            try:
                for i, path in enumerate(selection, 1):
                    self._set_job_progress(f"({i}/{len(selection)} {Path(path).stem})")
                    delete_batch(db, path,
                                 log_line=lambda m: self._ulog(f"[.sfen] {m}"))
            finally:
                self.root.after(0, self._refresh_sfen_list)
                self._after_db_update(db)

        self._db_jobs.put((".sfenバッチ削除", job))
        self._set_update_status()

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
        db_path = norm_db_path(self.db_var.get())
        sfen = self.sfen_var.get().strip()
        if not db_path or not sfen:
            return
        sources = self._enabled_sources()
        name_query = self._name_db_query
        self._search_running = True
        self.search_button.config(state=tk.DISABLED)
        self.more_button.config(state=tk.DISABLED)
        self.status_var.set("名前でDB検索中..." if name_query
                            else "絞り込みを反映中...")

        def task():
            try:
                started = time.perf_counter()
                if sources is not None:
                    self._wait_interval_prep(db_path)
                page = self._get_reader(db_path).precedents_page(
                    sfen, limit=PAGE_SIZE, sources=sources,
                    name_query=name_query)
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

    def _search_names_in_db(self) -> None:
        """対局者名の検索式をDB全体に適用して前例ページを取り直す。

        記法: スペース=AND / 大文字OR・`|`=OR / -語=除外 (ANDがORより強い)。
        各語は先手・後手どちらかの名前への部分一致 (大文字小文字無視)。
        入力しただけでは何も起きず、このボタンかEnterで初めて適用される。
        空欄で実行すると解除。レアな名前×前例100万局で数秒かかる。
        出典フィルタ・「さらに表示」とは合成されるが、局面を切り替えると
        解除される (検索式は残るので、ボタンで再適用できる)。適用中かどうかは
        ボタン横の「適用中」表示で分かる。"""
        if self.query_position is None or self._search_running:
            return
        self._name_db_query = self.name_filter_var.get().strip() or None
        self.name_applied_var.set("適用中" if self._name_db_query else "")
        self._refetch_precedents()

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
        before = (last.sort_key, last.game_id, last.ply)
        start_rank = last.rank
        db_path = norm_db_path(self.db_var.get())
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
                    sources=sources, start_rank=start_rank,
                    name_query=self._name_db_query)
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
        if self.post_x_button is not None:
            self.post_x_button.config(state=button_state)
        self.copy_kifu_button.config(state=tk.NORMAL)
        self.open_file_button.config(state=tk.NORMAL)
        lines = [f"{p.black_name} vs {p.white_name}  "
                 f"{p.started_at[:10].replace('-', '/')}  "
                 f"{RESULT_JA.get(p.result, '?')} ({REASON_JA.get(p.end_reason, p.end_reason)}) "
                 f"{p.ply_count}手"]
        detail = self._get_reader(norm_db_path(self.db_var.get())).get_game(p.game_id)
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
        reader = self._get_reader(norm_db_path(self.db_var.get()))
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
            reader = self._get_reader(norm_db_path(self.db_var.get()))
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
                self.search(quiet=True)

    # -- config ----------------------------------------------------------

    def _load_config(self) -> None:
        try:
            config = json.loads(CONFIG_PATH.read_text())
            self.db_var.set(config.get("db_path", ""))
            self.sfen_var.set(config.get("last_sfen", ""))
            self.startup_check_var.set(
                bool(config.get("floodgate_startup_check", True)))
            self.sync_var.set(bool(config.get("sync_follow", True)))
            # 旧設定からの移行: 対象DB未設定なら当時のビューアDB、それも
            # 無ければ既定パス (data/csa.db) を使う
            self.fg_db_var.set(config.get("floodgate_db_path")
                               or config.get("db_path")
                               or str(DEFAULT_PUBLIC_DB))
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
                "db_path": norm_db_path(self.db_var.get()),
                "last_sfen": self.sfen_var.get().strip(),
                "floodgate_startup_check": self.startup_check_var.get(),
                "sync_follow": self.sync_var.get(),
                "floodgate_db_path": norm_db_path(self.fg_db_var.get()),
                "private_db_path": norm_db_path(self.private_db_var.get()),
                "sfen_db_path": norm_db_path(self.sfen_db_var.get())},
                ensure_ascii=False))
        except OSError:
            pass


def main() -> None:
    # macOS同梱の 3.9 (Tk 8.5) はクリックを取りこぼす既知の不具合があり、
    # GUIとしては使いものにならないため起動を拒否する (KiriCompass.app は
    # 3.10+を自動で探すので、通常ここには来ない)。.app経由だとstderrが
    # 見えないため、可能ならダイアログでも通知する。
    if sys.version_info < (3, 10):
        message = (f"KiriCompassのGUIには Python 3.10 以上が必要です "
                   f"(現在: {sys.version.split()[0]})。macOS同梱の 3.9 は "
                   "tkinter (Tk 8.5) にクリック取りこぼしの不具合があります。")
        # pythonw (Windows) はstderrが見えないためダイアログでも通知する。
        # macOSの3.9は Tk 生成自体がabortし得るので試みない (stderrに出す)。
        if sys.platform == "win32":
            try:
                root = tk.Tk()
                root.withdraw()
                messagebox.showerror("KiriCompass", message)
            except Exception:  # noqa: BLE001 - 表示できなければstderrのみ
                pass
        sys.exit(message)
    setup_dpi_awareness()
    setup_file_logging()
    root = tk.Tk()
    PrecedentViewer(root)
    root.mainloop()


if __name__ == "__main__":
    main()
