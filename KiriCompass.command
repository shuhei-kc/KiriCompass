#!/bin/sh
# KiriCompass 前例ビューアの起動 (macOS/Linux ダブルクリック用)。
# 自分の場所を基準にするため、リポジトリをどこに置いても動く。
#
# Finderからの起動はPATHが最小 (/usr/bin優先) で、macOS同梱の
# Python 3.9 (Tk 8.5) を拾ってしまう。3.9のtkinterはクリックを
# 取りこぼす既知の不具合があるため、3.10以上の python3 を探して使う。
cd "$(dirname "$0")" || exit 1

PY=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/*/bin/python3 \
         python3; do
    command -v "$p" >/dev/null 2>&1 || continue
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
            2>/dev/null; then
        PY="$p"
        break
    fi
done

if [ -z "$PY" ]; then
    MSG="Python 3.10以上が見つかりません。python.org または Homebrew からインストールしてください。"
    echo "$MSG" >&2
    osascript -e "display alert \"KiriCompass\" message \"$MSG\"" >/dev/null 2>&1
    exit 1
fi

exec "$PY" tools/precedent_gui.py "$@"
