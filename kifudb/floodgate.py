"""floodgate (wdoor) の日別アーカイブから新規棋譜を逐次取り込む。

設計方針:
- 参照するのは終局棋譜の日別アーカイブ
  https://wdoor.c.u-tokyo.ac.jp/shogi/x/YYYY/MM/DD/ のみ (ライブページは
  叩かない)。実行は30分周期を想定しており、サーバ負荷はブラウザ観戦以下。
- ダウンロード要否は「games.event または source_files 台帳に記録があるか」
  で判定する。0手不成立などで登録されず削除されたファイルも台帳の行が
  墓標として残るため、二度と取りに行かない。
- 取り込み後の後始末: ok / duplicate / empty はファイル削除 (台帳行は保持)、
  unfinished は保持して次サイクルでリモートが変わっていれば再取得、
  error は調査のため保持する。
"""

from __future__ import annotations

import email.utils
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .db import open_read_only
from .ingest import IngestStats, ingest_folder

log = logging.getLogger("kifudb.floodgate")

BASE_URL = "https://wdoor.c.u-tokyo.ac.jp/shogi/x"
USER_AGENT = "KiriCompass-kifudb updater (github.com/shuhei-kc/KiriCompass)"
REQUEST_TIMEOUT = 30
DOWNLOAD_SLEEP = 0.2        # 連続ダウンロード間の小休止 (礼儀)
JST = timezone(timedelta(hours=9))  # wdoorの日別ディレクトリはJST基準

_CSA_HREF_RE = re.compile(r'href="([^"/]+\.csa)"')
_STEM_DATE_RE = re.compile(r"(\d{8})\d{6}$")

# 後始末の分類: ファイルを削除する台帳status (行は墓標として残す)
_DELETE_STATUSES = ("ok", "duplicate", "empty")


@dataclass
class UpdateResult:
    downloaded: int = 0         # 新規に取得したファイル数
    refreshed: int = 0          # 未終局の再取得数
    deleted: int = 0            # 取り込み後に削除したファイル数
    kept_unfinished: int = 0    # 保持した未終局ファイル数
    kept_error: int = 0         # 保持したエラーファイル数
    index_errors: int = 0       # インデックス取得に失敗した日数
    ingest: IngestStats | None = None

    def summary(self) -> str:
        parts = [f"新規DL {self.downloaded}", f"再取得 {self.refreshed}"]
        if self.ingest is not None:
            parts.append(f"取り込み ({self.ingest.summary()})")
        parts.append(f"削除 {self.deleted}")
        if self.kept_unfinished:
            parts.append(f"未終局保持 {self.kept_unfinished}")
        if self.kept_error:
            parts.append(f"エラー保持 {self.kept_error}")
        if self.index_errors:
            parts.append(f"インデックス失敗 {self.index_errors}日分")
        return " / ".join(parts)


# CA証明書バンドルのよくある場所 (python.org版PythonはOSの証明書ストアを
# 見ないため、既定コンテキストのCAが空になる環境がある)
_CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/cert.pem",                    # macOS (LibreSSL同梱)
    "/etc/ssl/certs/ca-certificates.crt",   # Debian/Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL系
)


def _ssl_context() -> ssl.SSLContext:
    """TLS検証に使うコンテキストを、環境に応じて多段フォールバックで作る。

    certifi → 既定ストア (CAが実際に入っている場合のみ) → OS標準のバンドル
    ファイル → 最後の手段として検証なし (明示的に警告を出す)。配布先で
    「Install Certificates.command を実行してください」と言わずに済ませる。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0):
        return context
    for cafile in _CA_BUNDLE_CANDIDATES:
        if Path(cafile).is_file():
            try:
                return ssl.create_default_context(cafile=cafile)
            except ssl.SSLError:
                continue
    log.warning("CA証明書ストアが見つからないため、TLS検証なしで接続します "
                "(pip install certifi で検証を有効にできます)")
    return ssl._create_unverified_context()  # noqa: SLF001


_SSL_CTX = _ssl_context()


def http_get(url: str, since_mtime: float | None = None) -> bytes | None:
    """GET。since_mtime を渡すと If-Modified-Since 付き。304/404 は None。

    UAの明示とTLSフォールバック (_ssl_context) 込みの共通ヘルパー。
    大会ダウンローダ (download_wcsc / download_denryusen) もこれを使う —
    外部ライブラリ (requests) に依存しないための一元化。"""
    headers = {"User-Agent": USER_AGENT}
    if since_mtime is not None:
        headers["If-Modified-Since"] = email.utils.formatdate(
            since_mtime, usegmt=True)
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT,
                                    context=_SSL_CTX) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (304, 404):
            return None
        raise


def _write_atomic(dest: Path, data: bytes) -> None:
    """一時ファイル経由で書いてからリネームする (原子的)。

    直接書くと、書き込み途中でプロセスが落ちたときに切断されたCSAが残り、
    しかもローカルmtimeが新しいため If-Modified-Since が304を返し続けて
    永久に修復されない。tmp経由なら中断してもdestは生まれず、次サイクルの
    「ローカルに無い」判定で取り直される。"""
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)


def list_day(day: date) -> list[str]:
    """日別インデックスから .csa ファイル名を列挙する (無い日は空リスト)。"""
    data = http_get(f"{BASE_URL}/{day:%Y/%m/%d}/")
    if data is None:
        return []
    html = data.decode("utf-8", errors="replace")
    names = {urllib.parse.unquote(m) for m in _CSA_HREF_RE.findall(html)}
    return sorted(names)


def _day_url(day_str: str, name: str) -> str:
    return (f"{BASE_URL}/{day_str[:4]}/{day_str[4:6]}/{day_str[6:8]}/"
            f"{urllib.parse.quote(name)}")


def days_behind(db_path: str | Path) -> int | None:
    """DB内の最新floodgate対局からの経過日数 (JST)。floodgate棋譜が無ければ None。

    起動時チェックの遡り日数をDB自身から決めるための補助。固定日数だと
    「久しぶりの起動で取りこぼす / 毎回無駄に遡る」の両方が起きる。"""
    conn = open_read_only(db_path)
    try:
        row = conn.execute("SELECT MAX(started_at) FROM games "
                           "WHERE source='floodgate'").fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    last = datetime.strptime(row[0][:10], "%Y-%m-%d").date()
    return max((datetime.now(JST).date() - last).days, 0)


def update_once(db_path: str | Path, mirror_dir: str | Path,
                days: int = 2, log_line=None,
                dates: "list[date] | None" = None) -> UpdateResult:
    """1サイクル実行: インデックス照合 → 新規DL → 取り込み → 後始末。

    `days` は今日から遡る日数 (既定2 = 日付をまたいで終局する対局への保険)。
    `dates` を渡すと代わりにその日付リストを走査する (過去の抜け監査用)。
    ネットワークエラーはその日をスキップして続行し、次サイクルで自然に
    回復する。DBへの書き込みは ingest_folder の1箇所のみ。
    """
    mirror = Path(mirror_dir)
    mirror.mkdir(parents=True, exist_ok=True)
    for stale in mirror.glob("*.tmp"):  # 中断された書き込みの残骸を掃除
        stale.unlink(missing_ok=True)
    result = UpdateResult()

    def say(message: str) -> None:
        log.info(message)
        if log_line:
            log_line(message)

    if dates is None:
        today = datetime.now(JST).date()
        dates = [today - timedelta(days=i) for i in range(days)]

    # --- 新規ファイルの検出とダウンロード (判定は読み取り接続) ---
    conn = open_read_only(db_path)
    try:
        for day in dates:
            try:
                names = list_day(day)
            except (urllib.error.URLError, OSError) as exc:
                result.index_errors += 1
                say(f"{day} のインデックス取得に失敗: {exc}")
                continue
            for name in names:
                dest = mirror / name
                if dest.exists():
                    continue  # 保持中の未終局/エラー分
                stem = name[:-4]
                if conn.execute("SELECT 1 FROM games WHERE event = ?",
                                (stem,)).fetchone():
                    continue  # 取り込み済み
                if conn.execute("SELECT 1 FROM source_files WHERE path = ?",
                                (str(dest),)).fetchone():
                    continue  # 墓標 (0手不成立などで登録されず削除済み)
                try:
                    data = http_get(f"{BASE_URL}/{day:%Y/%m/%d}/"
                                     f"{urllib.parse.quote(name)}")
                except (urllib.error.URLError, OSError) as exc:
                    say(f"ダウンロード失敗 {name}: {exc}")
                    continue
                if data is None:
                    continue
                _write_atomic(dest, data)
                result.downloaded += 1
                time.sleep(DOWNLOAD_SLEEP)

        # --- 保持中の未終局ファイルをリモートと突き合わせて再取得 ---
        mirror_resolved = mirror.resolve()
        for (path,) in conn.execute(
                "SELECT path FROM source_files WHERE status = 'unfinished'"):
            p = Path(path)
            if p.parent.resolve() != mirror_resolved or not p.is_file():
                continue  # ミラー外 (元コーパス由来) は対象外
            m = _STEM_DATE_RE.search(p.stem)
            if m is None:
                continue
            try:
                data = http_get(_day_url(m.group(1), p.name),
                                 since_mtime=p.stat().st_mtime)
            except (urllib.error.URLError, OSError) as exc:
                say(f"再取得失敗 {p.name}: {exc}")
                continue
            if data is not None:
                _write_atomic(p, data)  # mtime更新 → 台帳が自動的に再取り込み
                result.refreshed += 1
                time.sleep(DOWNLOAD_SLEEP)

        # クラッシュ等で台帳に載る前のファイルが残っていないか
        has_pending = any(
            conn.execute("SELECT 1 FROM source_files WHERE path = ?",
                         (str(f),)).fetchone() is None
            for f in mirror.glob("*.csa"))
    finally:
        conn.close()

    # --- 取り込み (唯一の書き込み) ---
    if result.downloaded or result.refreshed or has_pending:
        result.ingest = ingest_folder(db_path, mirror)

    # --- 後始末: 台帳statusに応じて削除/保持 ---
    conn = open_read_only(db_path)
    try:
        for f in sorted(mirror.glob("*.csa")):
            row = conn.execute(
                "SELECT status FROM source_files WHERE path = ?",
                (str(f),)).fetchone()
            status = row[0] if row else None
            if status in _DELETE_STATUSES:
                f.unlink()
                result.deleted += 1
            elif status == "unfinished":
                result.kept_unfinished += 1
            elif status == "error":
                result.kept_error += 1
            # status None (未取り込み) は次サイクルに委ねて保持
    finally:
        conn.close()

    say(result.summary())
    return result
