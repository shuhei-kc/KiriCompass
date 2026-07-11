#!/usr/bin/env python3
"""CLI: floodgateの新規棋譜を1回取り込んで終了する。

Usage:
    python3 tools/update_floodgate.py <db_path> [--jitter SEC] [--days N]

GUIの「DB更新」ウィンドウ (起動時チェック・今すぐ更新) と同じ
kifudb.floodgate.update_once を呼ぶだけの薄いラッパー。
DBが無ければ空で新規作成する。
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kifudb.db import open_for_write, resolve_db_path  # noqa: E402
from kifudb.floodgate import update_once  # noqa: E402

DEFAULT_MIRROR = Path(__file__).resolve().parent.parent / "data" / "floodgate"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path")
    parser.add_argument("--mirror", default=str(DEFAULT_MIRROR),
                        help=f"一時保存フォルダ (既定: {DEFAULT_MIRROR})")
    parser.add_argument("--days", type=int, default=2,
                        help="今日から遡って照合する日数 (既定: 2)")
    parser.add_argument("--jitter", type=int, default=0, metavar="SEC",
                        help="開始前に 0〜SEC 秒のランダムな待ちを入れる (既定: 0)")
    parser.add_argument("--log", default=None, help="ログファイル")
    args = parser.parse_args()

    handlers = [logging.StreamHandler()]
    if args.log:
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.jitter > 0:
        delay = random.uniform(0, args.jitter)
        logging.info("jitter: %.0f秒待機", delay)
        time.sleep(delay)

    # ヘッドレス初回運用: DBが無ければスキーマだけの空DBを作る
    # (update_once は読み取り接続から始まるため、無いと開けずに終わる)。
    if not resolve_db_path(args.db_path).is_file():
        logging.info("DBが無いため新規作成: %s", resolve_db_path(args.db_path))
        open_for_write(args.db_path).close()

    result = update_once(args.db_path, args.mirror, days=args.days)
    print(result.summary())


if __name__ == "__main__":
    main()
