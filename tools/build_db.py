#!/usr/bin/env python3
"""CLI: build or update a precedent database from a kifu folder.

Usage:
    python3 tools/build_db.py <db_path> <kifu_folder> [--log build.log]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kifudb.ingest import ingest_folder  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path")
    parser.add_argument("kifu_folder")
    parser.add_argument("--log", default=None, help="log file path")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--pv-max-moves", type=int, default=255,
                        help="store at most N moves per PV (0 = no PVs)")
    args = parser.parse_args()

    handlers = [logging.StreamHandler()]
    if args.log:
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")

    stats = ingest_folder(args.db_path, args.kifu_folder,
                          batch_size=args.batch_size,
                          pv_max_moves=args.pv_max_moves)
    print(stats.summary())


if __name__ == "__main__":
    main()
