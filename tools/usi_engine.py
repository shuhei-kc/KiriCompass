#!/usr/bin/env python3
"""前例DB USIエンジン: ShogiHome等のUSI GUIにダミーエンジンとして登録する。

起動:
    python3 tools/usi_engine.py --db /path/to/precedents.db

ShogiHomeへの登録用ラッパースクリプト生成 (これを登録する。
省略時は KC2/runtime/kifudb_engine.command に作られる):
    python3 tools/usi_engine.py --db /path/to/precedents.db --make-launcher

検討モードで使うと、局面ごとに前例の候補手がmultipvで表示され
(nodes列=出現局数、評価値=手番側の勝率換算)、閲覧中の局面が
syncファイルに書き出されて前例ビューアが追従できる。
"""

import argparse
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kifudb.usi import DEFAULT_SYNC_FILE, RUNTIME_DIR, PrecedentUsiEngine  # noqa: E402

DEFAULT_LAUNCHER = RUNTIME_DIR / "kifudb_engine.command"


def make_launcher(target: Path, args: argparse.Namespace) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    script_path = Path(__file__).resolve()
    lines = [
        "#!/bin/sh",
        f'exec "{sys.executable}" -u "{script_path}"'
        f' --db "{Path(args.db).resolve()}"'
        f' --sync-file "{Path(args.sync_file).resolve()}"'
        f" --multipv {args.multipv} --pv-depth {args.pv_depth}"
        + (f' --log "{Path(args.log).resolve()}"' if args.log else ""),
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"ラッパースクリプトを作成しました: {target}")
    print("ShogiHomeの「エンジン追加」でこのファイルを選択してください。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="", help="前例DBのパス")
    parser.add_argument("--sync-file", default=str(DEFAULT_SYNC_FILE),
                        help="閲覧中局面の書き出し先 (前例ビューアが追従)")
    parser.add_argument("--multipv", type=int, default=8)
    parser.add_argument("--pv-depth", type=int, default=12)
    parser.add_argument("--log", default=None, help="デバッグログの書き出し先")
    parser.add_argument("--make-launcher", nargs="?", const=str(DEFAULT_LAUNCHER),
                        default=None, metavar="PATH",
                        help="GUI登録用のラッパースクリプトを生成して終了 "
                             f"(省略時: {DEFAULT_LAUNCHER})")
    args = parser.parse_args()

    if args.make_launcher:
        if not args.db:
            parser.error("--make-launcher には --db が必要です")
        make_launcher(Path(args.make_launcher).expanduser(), args)
        return

    engine = PrecedentUsiEngine(
        db_path=args.db, sync_file=args.sync_file,
        multipv=args.multipv, pv_depth=args.pv_depth, log_file=args.log)
    engine.run()


if __name__ == "__main__":
    # GUIから起動された際に確実に行バッファで動くようにする
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
