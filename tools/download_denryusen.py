#!/usr/bin/env python3
"""電竜戦 (denryu-sen.jp) 棋譜ダウンローダ。

各大会フォルダのマニフェスト
    https://denryu-sen.jp/denryusen/<大会ID>/kifulist.txt
から対局(=CSAステム)を列挙し、
    https://denryu-sen.jp/denryusen/<大会ID>/kifufiles/<event>.csa
を data/denryusen/<大会ID>/<event>.csa として保存する。

ビューアURLの復元に必要な「event接頭辞 → 大会ID」の対応は不規則
(dr2prod / shishio3 / donou3 等) かつ同一フォルダに別接頭辞が混在するため、
ダウンロード実績 (どのフォルダから取れたか) から生成して map ファイルに書き出す。

Usage:
    python3 tools/download_denryusen.py                # 全大会
    python3 tools/download_denryusen.py dr6_production # 指定大会のみ
    python3 tools/download_denryusen.py --dry-run
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

BASE = "https://denryu-sen.jp/denryusen"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "denryusen"
MAP_OUT = Path(__file__).resolve().parent.parent / "data" / "denryusen_prefix_map.txt"

TOURNAMENTS = [
    "dr1_production",
    "dr2_production", "dr2_tsec", "dr2_exhi1", "dr2_exhi2",
    "dr3_production", "dr3_tsec", "dr3_hardware1", "dr3_sakura",
    "dr4_production", "dr4_tsec", "dr4_hardware2", "dr4_sakura",
    "dr4_patronage_do3",
    "dr5_production", "dr5_tsec", "dr5_hardware3",
    "dr6_production", "dr6_tsec",
    "dr7_tsec7",     # TSEC7 (2026-07開催。フォルダ名が drN_tsec 規則から外れる)
]

# 終局済みCSAの目印: %トークン行 (%TORYO等) か summary行。どちらも無い
# ローカルファイルは大会進行中に落とした未終局とみなし、再取得の対象にする。
_FINISHED_MARKERS = (b"\n%", b"'summary:")

_STEM_RE = re.compile(r'kifujs/([^"]+?)\.html')


def list_events(tid: str) -> list[str]:
    r = requests.get(f"{BASE}/{tid}/kifulist.txt", timeout=20)
    if r.status_code != 200:
        return []
    # 同一対局が複数行に出ることがあるので一意化 (順序保持)
    seen, out = set(), []
    for stem in _STEM_RE.findall(r.text):
        if stem not in seen:
            seen.add(stem)
            out.append(stem)
    return out


def _looks_finished(path: Path) -> bool:
    """ローカルCSAが終局済みらしいか (末尾に%トークンかsummary行があるか)。

    大会進行中にダウンロードした未終局の棋譜を、次回実行で取り直すための
    判定。終局済みなのに目印が無い古い例外 (初期TSECの技術的中断 約104局)
    は毎回再取得になるが、実害は無い。"""
    try:
        with open(path, "rb") as f:
            f.seek(max(path.stat().st_size - 4096, 0))
            tail = f.read()
        return any(marker in tail for marker in _FINISHED_MARKERS)
    except OSError:
        return False


def download_one(tid: str, event: str, retries: int = 3) -> bool:
    out = DATA_DIR / tid / f"{event}.csa"
    if out.exists() and out.stat().st_size > 0 and _looks_finished(out):
        return True
    url = f"{BASE}/{tid}/kifufiles/{event}.csa"
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
        except requests.RequestException:
            time.sleep(0.5 * (attempt + 1))  # 一時的な瞬断に備えて指数的に待つ
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(r.content)
        return True
    return False


def download_tournament(tid: str, dry_run: bool, prefix_counts: dict) -> tuple[int, int]:
    events = list_events(tid)
    for ev in events:
        prefix_counts[ev.split("+", 1)[0]][tid] += 1
    if dry_run:
        print(f"[{tid}] {len(events)} events  e.g. {events[0] if events else '-'}")
        return len(events), 0
    ok = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(download_one, tid, ev): ev for ev in events}
        for fut in as_completed(futs):
            if fut.result():
                ok += 1
    print(f"[{tid}] downloaded {ok}/{len(events)}")
    return len(events), ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tournaments", nargs="*", default=None,
                    help="省略時は全大会")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    targets = args.tournaments or TOURNAMENTS

    prefix_counts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    total = ok_total = 0
    started = time.time()
    for tid in targets:
        n, ok = download_tournament(tid, args.dry_run, prefix_counts)
        total += n
        ok_total += ok

    # event接頭辞 -> 最頻の大会ID を書き出す (query.py のマッピング生成用)。
    # 既存のmapファイルとマージする: 一部の大会だけ実行しても、他大会の
    # 実績が消えないように、今回走査した大会の列だけを置き換える。
    import ast
    merged: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    if MAP_OUT.is_file():
        for line in MAP_OUT.read_text(encoding="utf-8").splitlines():
            try:
                prefix, _tid, counts = line.split("\t")
                merged[prefix].update(ast.literal_eval(counts))
            except (ValueError, SyntaxError):
                continue
    for prefix, counter in prefix_counts.items():
        for tid in targets:            # 今回の実測でその大会の列を上書き
            merged[prefix].pop(tid, None)
        merged[prefix].update(counter)
    lines = []
    for prefix, counter in sorted(merged.items()):
        tid = counter.most_common(1)[0][0]
        lines.append(f"{prefix}\t{tid}\t{dict(counter)}")
    MAP_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nprefix map ({len(lines)} prefixes) -> {MAP_OUT}")
    print(f"total events={total} downloaded={ok_total} "
          f"in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
