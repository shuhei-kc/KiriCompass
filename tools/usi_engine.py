#!/usr/bin/env python3
"""前例DB USIエンジン: 将棋盤GUI (USI対応) にダミーエンジンとして登録する。

起動:
    python3 tools/usi_engine.py --db /path/to/csa.db

将棋盤GUIへの登録用ラッパースクリプト生成 (これを登録する。
省略時は KC2/runtime/kifudb_engine.command に作られる):
    python3 tools/usi_engine.py --db /path/to/csa.db --make-launcher

検討モードで使うと、局面ごとに前例の候補手がmultipvで表示され
(nodes列=出現局数、読み筋=最頻前例の続き。評価値は意味を持たないので常に0)、
閲覧中の局面がsyncファイルに書き出されてKiriCompassビューアが追従できる。
"""

import argparse
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kifudb.usi import DEFAULT_SYNC_FILE, RUNTIME_DIR, PrecedentUsiEngine  # noqa: E402

IS_WINDOWS = sys.platform == "win32"
DEFAULT_LAUNCHER = RUNTIME_DIR / (
    "kifudb_engine.bat" if IS_WINDOWS else "kifudb_engine.command")


def make_launcher(target: Path, args: argparse.Namespace) -> None:
    """OSに合わせたエンジン起動スクリプトを生成する。

    Windows: .bat (ShogiHomeは .bat/.cmd を cmd.exe /c で起動する)
    macOS/Linux: シェルスクリプト (.command は Finder からも実行可能)
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    script_path = Path(__file__).resolve()
    common_args = (
        f' --db "{Path(args.db).resolve()}"'
        f' --sync-file "{Path(args.sync_file).resolve()}"'
        f" --multipv {args.multipv} --pv-depth {args.pv_depth}"
        f" --encoding {args.encoding}"
        + (f' --log "{Path(args.log).resolve()}"' if args.log else ""))
    if IS_WINDOWS:
        content = (f'@echo off\r\n'
                   f'"{sys.executable}" -u "{script_path}"{common_args}\r\n')
        # cmd.exe はバッチをANSI/OEMコードページで解釈するため、
        # 日本語を含むパスに備えて mbcs (日本語環境では cp932) で書く。
        target.write_text(content, encoding="mbcs", errors="replace")
    else:
        content = (f'#!/bin/sh\n'
                   f'exec "{sys.executable}" -u "{script_path}"{common_args}\n')
        target.write_text(content, encoding="utf-8")
        target.chmod(target.stat().st_mode
                     | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"ラッパースクリプトを作成しました: {target}")
    print("将棋盤GUIの「エンジン追加」でこのファイルを選択してください。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="", help="前例DBのパス")
    parser.add_argument("--sync-file", default=str(DEFAULT_SYNC_FILE),
                        help="閲覧中局面の書き出し先 (ビューアが追従)")
    parser.add_argument("--multipv", type=int, default=8)
    parser.add_argument("--pv-depth", type=int, default=12)
    parser.add_argument("--encoding", choices=("utf-8", "cp932"),
                        default="utf-8",
                        help="GUIへの出力エンコーディング "
                             "(既定utf-8 / ShogiGUI・将棋所は cp932)")
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
    # ShogiGUI・将棋所向け (cp932) は起動フラグで、あるいはGUIの
    # setoption OutputEncoding からも実行中に切り替えられる。
    engine.set_output_encoding(args.encoding)
    engine.run()


if __name__ == "__main__":
    # GUIから起動された際に確実に行バッファで動くようにする
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    # ShogiHome (Node readline) はUTF-8でエンジン出力を読むため、
    # Windowsのcp932既定に引きずられないよう既定はUTF-8に固定する
    # (--encoding cp932 で上書き可能)。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    main()
