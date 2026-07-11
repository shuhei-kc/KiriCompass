#!/usr/bin/env python3
"""WCSC / WCSO 棋譜ダウンローダ (配信元のファイル名を保持する)。

旧 attic/wcsc_d.py は list.txt 由来の棋譜を対局者名ベースにリネームしていた
が、それだと棋譜ビューア URL (/kifu/<元名>.html) の復元に必要な「元の短縮
ファイル名」(例: WCSC32_F7_YAN_TNK_1) が失われてしまう。

このスクリプトは常に配信元のファイル名のまま保存する:
  - 28〜35, WCSO1 : live4 の list.txt を使用
      * 33〜35 の list.txt はフル名 (WCSC33+...+timestamp) を配信
      * 28〜32・WCSO1 の list.txt は短縮名 (WCSC32_F7_YAN_TNK_1) を配信
  - 36 (list.txt 無し) : S3 バケットを使用 (フル名)
どちらも各大会の棋譜ビューア URL のステムと一致する。

Usage:
    python3 tools/download_wcsc.py 28 29 31 32 33 34 35 36 wcso1
    python3 tools/download_wcsc.py --dry-run 36        # 件数だけ確認
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error
from pathlib import Path
from urllib.parse import unquote, urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kifudb.floodgate import http_get  # noqa: E402 - UA/TLS込みの標準ライブラリGET

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "wcsc"

_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)


def _get_text(url: str) -> str | None:
    """GETしてテキストを返す。404/接続失敗は None。"""
    try:
        data = http_get(url)
    except (urllib.error.URLError, OSError):
        return None
    return None if data is None else data.decode("utf-8", errors="replace")


def edition_paths(edition: str) -> tuple[str, str, str]:
    """(live4スラッグ, 保存フォルダ名, S3バケット名) を返す。"""
    if str(edition).lower() == "wcso1":
        return "wcso1", "wcsc_kifu_wcso1", "k-wcso1"
    n = int(edition)
    return f"wcsc{n}", f"wcsc_kifu{n}", f"k-wcsc{n}"


def list_txt_urls(slug: str) -> list[str]:
    base = f"http://live4.computer-shogi.org/{slug}/"
    text = _get_text(urljoin(base, "list.txt"))
    if text is None:
        return []
    return [urljoin(base, s.strip()) for s in text.splitlines()
            if s.strip().lower().endswith(".csa")]


def s3_urls(bucket: str) -> list[str]:
    base = f"https://{bucket}.s3.amazonaws.com/"
    for prefix in ("live", "test"):
        text = _get_text(f"{base}{prefix}1.html")
        if text is None:
            continue
        # ページ総数は h1 の「n / total」表記から (タグ1個なので正規表現で足りる)
        total = 0
        h1 = _H1_RE.search(text)
        if h1:
            m = re.search(r"\s*\d+\s*/\s*(\d+)", h1.group(1))
            if m:
                total = int(m.group(1))
        urls: list[str] = []
        for i in range(1, max(total, 1) + 1):
            page = text if i == 1 else _get_text(f"{base}{prefix}{i}.html")
            if page is None:
                continue
            mf = re.search(r'const KIF_FILES = "([^"]+)"', page)
            if mf:
                urls += [urljoin(base, fn.strip().lstrip("./"))
                         for fn in mf.group(1).split(",") if fn.strip()]
        if urls:
            return urls
    return []


def resolve_urls(edition: str) -> tuple[list[str], str]:
    slug, _, bucket = edition_paths(edition)
    urls = list_txt_urls(slug)
    if urls:
        return urls, "list.txt"
    return s3_urls(bucket), "S3"


def download_edition(edition: str, dry_run: bool = False) -> int:
    _, folder, _ = edition_paths(edition)
    urls, src = resolve_urls(edition)
    # 配信元では同一対局が複数URLで重複列挙されることがあるので名前で一意化
    by_name = {Path(unquote(u)).name: u for u in urls}
    out = DATA_DIR / folder
    print(f"[{edition}] source={src} unique_files={len(by_name)} -> {out}")
    if dry_run:
        for name in list(by_name)[:3]:
            print(f"    e.g. {name}")
        return len(by_name)
    out.mkdir(parents=True, exist_ok=True)
    ok = 0
    for name, u in by_name.items():
        try:
            data = http_get(u)
            if data is None:
                raise urllib.error.URLError("404 not found")
            (out / name).write_bytes(data)
            ok += 1
        except (urllib.error.URLError, OSError) as e:
            print(f"    FAIL {name}: {e}")
        time.sleep(0.03)
    print(f"[{edition}] downloaded {ok}/{len(by_name)}")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("editions", nargs="+", help="28 29 31 ... 36 wcso1")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    for ed in args.editions:
        download_edition(ed, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
