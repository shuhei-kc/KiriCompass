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

## API 用の PC でやること (次)

1. この版の `kifudb` を API 用の PC に入れる (サーバ `backend/server.py` が読む `kifudb/query.py` を新しくする)。
2. `/api/precedents` に省略できる引数 `q` を足し、`precedents_page(name_query=q)` に渡す。省略したら今と同じ。
3. 応答に `q` をそのまま返す (`"q": "b:Frieren"`)。画面は、送った条件と違う応答を捨てる (今の `sfen` の照合と同じ)。
4. 続き (`cursor`) は条件ごと。条件が変わった続きの要求は断るか、最初のページを返す (別の条件のページを混ぜない)。
5. 名前で絞ると、前例の多い局面 (初期局面など) では数秒かかることがある。打ち切り (例: 2 秒) を持ち、
   打ち切ったら `truncated: true` とそれまでの分を返す。毎秒 8 件の制限で、絞り込みを 1 件で 2 件分に数えるかも決める。
6. 反映したら、KiriCanvas 側で `q` を送る作業に入る (KiriCompass_Lab の `kiricanvas/docs/PROPOSALS.md` の C)。

## まだ決めていないこと (後で)

- 期間・結果・手数の下限での絞り込み (`since` / `until` / `result` / `minPly`)。
- 絞り込んだときの候補手の集計 (`/api/candidates` にも `q`)。全局の集計 (`position_stats`) は今のまま使う。
- 名前で絞ったときの速さの改善 (対局者の表を足し、当てはまる対局と局面の前例の少ない方から引く。`position_games` に索引は足さない)。
- 名前の候補 (`/api/players?q=`)。
