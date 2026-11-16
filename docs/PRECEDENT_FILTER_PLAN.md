# 前例の絞り込み: 方針 (2026-11-10)

KiriCanvas から「先手が Frieren の対局だけ」のように前例を絞り込むための方針。別のセッション (API 用の PC の作業など) は、これを読んでから進める。

## 決めたこと

- **記法**: 今の対局者名の検索式に `b:` (先手) / `w:` (後手) を足す。全角の接頭辞 (`先:` など) は使わない。
  例: `b:Frieren`、`b:Frieren w:Sora_Ginko`、`Frieren -b:Frieren` (後手の Frieren だけ)、`b:Frieren OR w:Frieren`。
  接頭辞の無い語は今と同じ (先手・後手どちらかの部分一致)。
- **進め方**: KiriCompass (このリポジトリの `kifudb`) を直す → API 用の PC に反映する → KiriCanvas が使う。
- KiriCanvas の画面では、絞り込み中は前例の見出しに条件の札 (例「先手 Frieren ×」) を出す。

## 済んだこと

- `kifudb/query.py` の `_name_query_condition` に `b:` / `w:` (大文字も可、除外は `-b:語`) を足した。
  `precedents_page(name_query=...)` と前例ビューア (`tools/precedent_gui.py`) はそのまま使える。テストは `tests/test_name_query.py`。
- API (`backend/server.py` の `/api/precedents`) に `q` を足し、2026-11-10 に API 用の PC へ反映した。下の「API の仕様」のとおりに動いている。

## API の仕様 (`/api/precedents?q=`)

KiriCanvas 側はこれに合わせて作る。

- `q` は省略できる。省略 (または空白だけ) なら、応答は `q` を足す前と同じ。
- `q` を付けたときの違い:
  - 応答に `q` をそのまま返す (`"q": "b:Frieren"`)。画面は、送った条件と違う応答を捨てる (`sfen` の照合と同じ)。
  - `total` は返さない。条件に合う総数は、全部を読まないと分からない。
  - 1 回の要求で読むのは 2 秒まで。区切ったときは `truncated: true` と、そこまでに見つけた分、`nextCursor` を返す。
    `nextCursor` は「読み終えた位置」なので、0 件で区切られても、続きを頼めば必ず先へ進む。
    `nextCursor` が無ければ、末尾まで読み終えている。
  - 続き (`cursor`) は条件と並び順ごと。別の条件や並び順で取った `cursor` は 400 で断る。
    `cursor` の中身は決まった形に頼らず、返ってきたものをそのまま送る。
  - 区切った応答はサーバ側で溜めない。区切らなかった応答は、DB が変わるまで使い回す。
- 式は 200 文字・12 語まで。名前の無い式 (`OR` だけなど) は 400。
- 混雑への備え: 名前で絞る検索は同時に 2 本まで (空かなければ 503)。毎秒 8 件の制限では、1 件を 2 件分に数える。
- 計画と変えたところ: API 用の PC の `kifudb` は入れ替えていない。サーバは、読み込んだ `kifudb` が `b:` / `w:` を
  知っていればその `_name_query_condition` を使い、知らなければサーバ内の同じ写しを使う。どちらを使っているかは
  `/api/health` の `nameQuery` (`core` / `builtin`) に出る。**記法を変えるときは、サーバ内の写しも一緒に直す。**
- 実測 (API 用の PC、初期局面 = 前例 約 110 万局): 当たりの無い名前で全部を読んで約 1.4 秒。
  このため本番では 2 秒の区切りはまだ起きておらず、区切りの動きは手元の試験でしか確かめていない。

## 次にやること

- KiriCanvas 側で `q` を送る (KiriCompass_Lab の `kiricanvas/docs/PROPOSALS.md` の C)。

## まだ決めていないこと (後で)

- 期間・結果・手数の下限での絞り込み (`since` / `until` / `result` / `minPly`)。
- 絞り込んだときの候補手の集計 (`/api/candidates` にも `q`)。全局の集計 (`position_stats`) は今のまま使う。
- 名前で絞ったときの速さの改善 (対局者の表を足し、当てはまる対局と局面の前例の少ない方から引く。`position_games` に索引は足さない)。
  上の実測のとおり今は足りているので、急がない。
- 名前の候補 (`/api/players?q=`)。
