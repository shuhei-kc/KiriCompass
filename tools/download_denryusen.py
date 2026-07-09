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

# dr7_tsec7 は開催前のため既定では対象外。
TOURNAMENTS = [
    "dr1_production",
    "dr2_production", "dr2_tsec", "dr2_exhi1", "dr2_exhi2",
    "dr3_production", "dr3_tsec", "dr3_hardware1", "dr3_sakura",
    "dr4_production", "dr4_tsec", "dr4_hardware2", "dr4_sakura",
    "dr4_patronage_do3",
    "dr5_production", "dr5_tsec", "dr5_hardware3",
    "dr6_production", "dr6_tsec",
]

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


def download_one(tid: str, event: str, retries: int = 3) -> bool:
    out = DATA_DIR / tid / f"{event}.csa"
    if out.exists() and out.stat().st_size > 0:
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

    # event接頭辞 -> 最頻の大会ID を書き出す (query.py のマッピング生成用)
    lines = []
    for prefix, counter in sorted(prefix_counts.items()):
        tid = counter.most_common(1)[0][0]
        lines.append(f"{prefix}\t{tid}\t{dict(counter)}")
    MAP_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nprefix map ({len(lines)} prefixes) -> {MAP_OUT}")
    print(f"total events={total} downloaded={ok_total} "
          f"in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
