# KiriCompass — 前例データベース (kifudb)

コンピュータ将棋の棋譜 (floodgate / WCSC / 電竜戦 / KIF) から前例データベースを
構築し、局面 (sfen) から候補手と前例対局を高速検索するツール群。
ShogiHomeにUSIエンジンとして登録して、検討中の局面の前例をリアルタイムに
閲覧できる。依存は Python 3.10+ の標準ライブラリのみ。
設計の詳細は [DESIGN.md](DESIGN.md)。

## 使い方

DBの作成・更新 (増分。同じフォルダに再実行しても安全):

```bash
python3 tools/build_db.py precedents.db /path/to/kifu_folder --log build.log
```

前例ビューア (sfenを貼り付けて検索するテストGUI):

```bash
python3 tools/precedent_gui.py precedents.db
```

ShogiHome連動 (USIダミーエンジン):

```bash
# 登録用ラッパースクリプトを runtime/ に生成し、ShogiHomeの「エンジン追加」で選択する
python3 tools/usi_engine.py --db precedents.db --make-launcher
```

ShogiHomeの検討モードで使うと、局面ごとに前例の候補手がmultipvで表示される
(nodes列=出現局数、読み筋=最頻前例の続き。評価値は意味を持たないので常に0)。
同時に閲覧中の局面がsyncファイル (`runtime/sync_position.json`) に書き出され、
前例ビューアの「ShogiHome連動」をONにすると自動追従する。
ランチャー・syncファイル・GUI設定などの実行時ファイルはすべて `runtime/` に入る。
エンジン・ビューアはそれぞれ単体でも動く (連動は一方向・疎結合)。
前例がない局面では `bestmove resign` を返す (定跡専用エンジンとして
「知識が尽きた」ことを正直に伝える挙動。検討モードではbestmoveは実質無視される)。

テキストレポート出力:

```bash
python3 tools/lookup.py precedents.db "lnsgkgsnl/1r5b1/... b - 1" --out report.txt
```

## メモ

- 未終局の棋譜は取り込まれず、ファイル更新後の再実行で自動的に再チェックされる。
  0手のファイルは対局不成立 (aborted) として別カウントされる。
- 読み筋を保存したくない場合は `build_db.py --pv-max-moves 0`。
- 評価値は先手視点。前例ビューアで前例を選択すると評価値・読み筋を表示する。
- 検索はプロセス内で接続を使い回してキャッシュを温存する (エンジン・GUIとも対応済み)。
  外部SSD上のDBで初回や放置後のアクセスだけ遅いのはドライブのスリープ/コールド
  リードによるもので、2回目以降は速くなる。
- DB構築はメモリを最大でも数百MB程度しか使わない (SQLiteのページキャッシュ256MB
  + 作業用セット)。マシンのメモリ使用量が増えて見えるのは主にOSのファイル
  キャッシュで、問題ない。
