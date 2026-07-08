#!/usr/bin/env python3
"""CLI: look up precedents for a position and print/save a text report.

Usage:
    python3 tools/lookup.py <db_path> "<sfen>" [--out report.txt] [--limit 500]

The move-counter part of the sfen is ignored, as are leading
"position sfen" / "sfen" prefixes. "startpos" is also accepted.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kifudb.query import format_report, lookup  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path")
    parser.add_argument("sfen")
    parser.add_argument("--out", default=None, help="write report to this file")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()

    candidates, precedents, total = lookup(args.db_path, args.sfen, args.limit)
    report = format_report(args.sfen, candidates, precedents, total)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"saved: {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
