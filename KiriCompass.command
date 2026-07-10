#!/bin/sh
# KiriCompass 前例ビューアの起動 (macOS/Linux ダブルクリック用)。
# 自分の場所を基準にするため、リポジトリをどこに置いても動く。
cd "$(dirname "$0")" || exit 1
exec python3 tools/precedent_gui.py "$@"
