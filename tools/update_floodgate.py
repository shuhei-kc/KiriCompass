#!/usr/bin/env python3
"""CLI: floodgateの新規棋譜を1回取り込んで終了する (cron等のヘッドレス運用向け)。

Usage:
    python3 tools/update_floodgate.py <db_path> [--mirror DIR] [--days N]

毎時 :20/:50 起点 + ランダムな待ち での実行を推奨 (対局は毎時 :00/:30 開始。
利用者全員が同一秒に集中しないための分散。READMEの sleep 例を参照)。
GUIの「DB更新」ウィンドウと同じ kifudb.floodgate.update_once を
呼ぶだけの薄いラッパー。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kifudb.floodgate import update_once  # noqa: E402

DEFAULT_MIRROR = Path(__file__).resolve().parent.parent / "data" / "floodgate"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path")
    parser.add_argument("--mirror", default=str(DEFAULT_MIRROR),
                        help=f"一時保存フォルダ (既定: {DEFAULT_MIRROR})")
    parser.add_argument("--days", type=int, default=2,
                        help="今日から遡って照合する日数 (既定: 2)")
    parser.add_argument("--log", default=None, help="ログファイル")
    args = parser.parse_args()

    handlers = [logging.StreamHandler()]
    if args.log:
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")

    result = update_once(args.db_path, args.mirror, days=args.days)
    print(result.summary())


if __name__ == "__main__":
    main()
