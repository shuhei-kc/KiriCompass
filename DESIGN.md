# 前例データベース設計書 (kifudb)

2026-07-10 / KC2 第3版 (スキーマv3・GUI・floodgate逐次更新・.sfen対応を反映)

## 目的

KiriCompass の前例検索基盤を1から作り直す。要件は、随時更新中も閲覧できること、
大規模アクセスに耐えること、容量最小、メモリほぼ不使用での高速検索、
元棋譜ファイルなしで完結すること、環境非依存であること。

## 現行 (KC1 cache.py) の問題点と対応

現行は1局面1行に前例リスト全体をJSON文字列で保存するため、1局の情報が
経由局面の数だけ複製され、100万局で約50GBに膨張していた。カラム名が日本語、
python-shogi 依存のため CSA V2.2 (WCSC) が読めない、WALなしで更新中の閲覧が
不安定、終局理由の判定がアドホック、未終局ファイルの混入防止なし、という
問題もあった。新設計ではゲーム情報を `games` に1回だけ持ち、局面側は整数のみの
行にすることで、実測で **1/12 の容量** になる。

## スキーマ

SQLite 1ファイル。`PRAGMA journal_mode=WAL` で書き込み中も読み取り可能。
閲覧側は常に `mode=ro` で接続する。カラム名はすべて英語。

```sql
games (
    game_id      INTEGER PRIMARY KEY,
    event        TEXT UNIQUE,   -- 大会・サーバー上の対局ID (ファイル名語幹) = 重複排除キー
    source       TEXT,          -- floodgate / wdoor / wcsc / denryusen / other
    started_at   TEXT,          -- ISO 8601 ("2026-07-04 07:00:00")
    black_name   TEXT,          -- 棋譜内 N+ (ファイル名側の名前は event が保持)
    white_name   TEXT,          -- 棋譜内 N-
    result       INTEGER,       -- 0=引分 1=先手勝 2=後手勝 NULL=結果なし(中断等)
    end_reason   TEXT,          -- 正規化トークン ('toryo','sennichite',...)
    ply_count    INTEGER,
    initial_sfen TEXT,          -- NULL=平手初期局面。駒落ち・任意局面はここに保持
    moves        BLOB           -- 16bit手コード列 (cshogi move16 互換, LE)
)

position_games (               -- 前例インデックス本体
    position_key INTEGER,      -- 局面の64bitハッシュ (手数無視・符号付き格納)
    sort_key     INTEGER,      -- 対局開始時刻 (epoch分, 不明=0)
    game_id      INTEGER,
    ply          INTEGER,      -- この局面に到達した時点の手数 (0=初期局面)
    next_move    INTEGER,      -- 次の一手 move16, 0=ここで終局
    PRIMARY KEY (position_key, sort_key, game_id, ply)
) WITHOUT ROWID
-- 日付をキーに含めるのが重要: 「新しい順に上位N件」が索引の逆走査だけで
-- 取れるため、前例100万件の局面でも読むのはN行分のみ (結合・ソート不要)。
-- v2ではここで全前例の結合+ソートが走り、初期局面の初回検索が数十秒
-- かかっていた。旧DBは次回のDB更新実行時に自動移行される (schema v3)。

position_stats (               -- 候補手集計。2局以上が到達した局面のみ保持
    position_key INTEGER, next_move INTEGER,
    game_count, black_wins, white_wins, draws,
    PRIMARY KEY (position_key, next_move)
) WITHOUT ROWID

game_analysis (                -- 評価値・読み筋 (記録がある対局のみ)
    game_id INTEGER PRIMARY KEY,
    evals BLOB,                -- int16/局面スロット, -32768=記録なし, 先手視点
    pvs   BLOB                 -- zlib圧縮 (スロットごと: 長さ1B + move16列)
)

source_files (                 -- 取り込み台帳 (増分更新・未終局の再チェック用)
    path TEXT PRIMARY KEY, file_size, file_mtime_ns,
    status TEXT,               -- ok / unfinished / duplicate / error
    detail, game_id, ingested_at
)
```

## 主要な設計判断

**局面キー = 64bitハッシュのみ。** 局面の正規化sfen (盤・手番・持駒、手数は除外) の
blake2b 8バイト。局面そのものは保存しない。誤ヒット期待値は1億局面規模でも
1件未満で、表示時に `games.moves` を再生して照合すれば実質ゼロになる
(プロトタイプで照合パスを実証済み)。ライブラリやプラットフォームに依存しない
決定的なキーである点も重要 (cshogi の zobrist に将来差し替え可能だが、
再構築なしで移行できないため blake2b を正とする)。

**DBは自己完結。** `moves` に全指し手を保持するため、元ファイルなしで
盤面再生・棋譜書き出し・照合ができる。約240B/局の追加で済む。

**対局者名は両方保持。** 棋譜内の表記 (`black_name`/`white_name`) と、
ファイル名・大会側のログイン名 (`event` に含まれる) を別々に持つ。

**棋譜URL生成規則** (query.py `game_url`):

| 出典 | URL |
|---|---|
| floodgate / wdoor | `https://wdoor.c.u-tokyo.ac.jp/shogi/x/YYYY/MM/DD/{event}.html` |
| 電竜戦 | `https://denryu-sen.jp/denryusen/{大会ID}/dist/#/{event}/{手数}` |
| WCSC26以降(25含む) | `http://live4.computer-shogi.org/wcsc{NN}/html/{event}.html` |
| WCSC17〜24 | `http://live2.computer-shogi.org/wcsc{NN}/html/{event}.html` |
| WCSC16以前 | 対局ページなし (アーカイブ: www2.computer-shogi.org/kifu/kifu.html) |

電竜戦の大会IDは event 接頭辞から変換が必要: `dr5prd`→`dr5_production`、
`dr3hd1`→`dr3_hardware1`。ビューアは末尾に手数アンカーを取れるため、
前例の該当局面 (ply) を付けて開く。

WCSC過去大会の既知の注意点 (やねうら王氏の調査より、将来のダウンローダ用):
WCSC23の list.txt 内URLは `live.` 表記だが実体は `live2.` に移転済みで要置換。
WCSC16の中継リンク (homepage.mac.com) はdead linkで、WCSC16以前の棋譜は
www2.computer-shogi.org/kifu/kifu.html から。WCSC6以前の .lzh は lh4圧縮で
Python の lhafile 非対応。WCSC25以前の中継ページは右半分の表示が崩れるが
棋譜ダウンロードは可能。

**候補手集計はハイブリッド。** 2局以上が到達した局面のみ `position_stats` に
事前集計 (初期局面のような超頻出局面も即応答)。単発局面 (全局面キーの約94%) は
行を持たず、検索時に `position_games` を数行 GROUP BY するだけなので遅くならない。
これで集計テーブルが 6.2MB→0.4MB (実測) に縮んだ。集計は取り込み時に
`position_games` から再計算するため、増分更新でも常に正しい。

**終局理由と勝敗の判定順序。** (1) floodgate/電竜戦の `'summary:` 行を最優先
(勝敗を名前で照合)、(2) `%TORYO` 等の特殊手 — TORYO/TIME_UP/TSUMI は手番側の負け、
KACHI は手番側の勝ち、ILLEGAL_MOVE 系は直前に指した側の負け、
`%±ILLEGAL_ACTION` は符号側の負け、千日手・持将棋・最大手数は引き分け。
中断・エラーは result NULL のまま取り込む (序盤の前例としては有効なため)。

**評価値・読み筋は先手視点でスロット格納。** floodgate・WCSC・電竜戦の `'**`
コメントは「p手目とp+1手目の間のコメント = p手時点の局面の探索情報、評価値は
先手視点」で一貫していることを実データで検証した (勝敗との符号一致を全対局で
確認、読み筋の初手と実際の次手の一致率でスロット規約を確認)。読み筋はCSA形式・
USI形式どちらのトークンも受け付け、解釈不能なトークン以降は切り捨てる。
評価値+数値以外の独自コメント (フリーテキスト) は正規表現に一致しないため
自然に除外される。KIF系は ShogiHome / ShogiGUI / 棋神アナリティクス /
K-Shogi・ぴよ将棋 の各記法に対応 (shogihome の comment.ts を参考にした)。
読み筋は `--pv-max-moves` で長さ制限 (0で評価値のみ) が可能。

**未終局ファイルは取り込まない。** 終局トークンも summary もないファイルは
status='unfinished' で台帳にだけ記録し、サイズ/mtime が変わったら再チェックする。
全処理は logging でファイルとGUIログ両方に出す。

**増分更新。** 台帳の (size, mtime_ns) が一致するファイルはパースせずスキップ。
`event` の UNIQUE 制約で別フォルダの同一棋譜も重複排除。私的棋譜
(source=other) は event がファイル名語幹で一意性の保証がないため、指し手
内容のハッシュ8桁を event に付加する — 同名の別対局は両方登録され、
同一内容の複製だけが排除される。

**出典判定は命名構造まで要求する。** 公開/プライベートの振り分けに使うため、
接頭辞一致 (`^dr\d` 等) では私的なファイル名が公開DBへ紛れ込む余地がある。
floodgate は `wdoor+…+開始時刻14桁`、WCSC/WCSO は `wcs[co]NN` + 区切り
(例外: WCSC28決勝の `WCSC_F1_…`)、電竜戦は `<接頭辞>+…+開始時刻14桁` を
要求する (詳細は ingest.py detect_source)。全1,080,482件の実eventで
従来分類との一致を検証済み。

## 実測 (このリポジトリの実データ)

floodgate 2026/07 (1,958ファイル) + WCSC35 (289) + 電竜戦dr6 (526) +
棋王戦KIF (1) を取り込み。

| 項目 | 結果 |
|---|---|
| 取り込み | 2,762局 / 約16秒 (純Python)。未終局12件検出、エラー0 |
| DBサイズ (評価値・読み筋込み) | 19.9MB = 7,190B/局 → **100万局換算 約7.2GB** |
| 内訳 | 前例索引 8.0MB / 評価値・読み筋 8.9MB / games 1.2MB |
| 参考: 読み筋なし (`--pv-max-moves 0` 相当) | 約4GB/100万局 (現行50GBの約1/12) |
| 検索 (初期局面: 前例3,285局) | 5.9ms (候補31手+前例500件取得) |
| 検索 (終盤の単発局面) | 0.3ms |
| 再実行 (全ファイル変更なし) | 0.25秒 |
| 書き込みトランザクション中の読み取り | 0.5ms (WAL) |

WCSC35 の V2.2、電竜戦の buoy (途中局面開始)、PI駒落ち・P1-P9任意局面・AL持駒、
KIFの手合割 (二枚落ち等)・BOD任意局面・「不成」表記・玉/王・龍/竜の揺れも
パーサーが処理することをテスト済み。

## パッケージ構成

```
KiriCompass/
  kifudb/
    board.py        局面再生・sfen入出力・move16・日本語表記・局面キー
    csa.py          CSAパーサー (V2/V2.2, summary, 未終局検出, 評価値・読み筋)
    kif.py          KIFパーサー (手合割・BOD・表記揺れ・解析コメント各種)
    ki2.py          KI2表記の出力 (候補手・読み筋の日本語表示)
    analysis.py     評価値・読み筋のエンコード (int16 / zlib+move16)
    db.py           スキーマ・接続 (WAL / read-only / v2→v3移行)
    ingest.py       フォルダ走査→増分取り込み・出典判定・集計更新・ログ
    sfen_ingest.py  .sfen連続対局の取り込み・バッチ管理
    floodgate.py    floodgate日別アーカイブの逐次取り込み
    export.py       DBからのCSA棋譜復元
    query.py        sfen→候補手+前例、game_id→全記録 (URL生成含む)
    usi.py          USIダミーエンジン本体 (syncファイル書き出し)
  tools/
    build_db.py            DB作成・更新CLI
    update_floodgate.py    floodgate逐次更新CLI (cron等のヘッドレス運用向け)
    download_wcsc.py       WCSC棋譜ダウンローダ
    download_denryusen.py  電竜戦棋譜ダウンローダ
    precedent_gui.py       前例ビューア (sfen検索 + 将棋盤GUI追従 + DB更新)
    usi_engine.py          USIエンジン起動 (将棋盤GUI登録用、--make-launcher対応)
```

## 将棋盤GUI連動 (kifudb/usi.py)

USIプロトコルのダミーエンジンとして登録し、一方向連動する。`position` を
受けるたびに局面をsyncファイル (JSON, アトミック書き換え) へ書き出し、
前例ビューアがmtimeポーリング (300ms) で追従する。プロセス間はファイル1個
だけの疎結合なので、エンジン・ビューアどちらも単体起動できる。

`go` への応答は前例の実データ: 候補手を出現数順に `info multipv` で返し、
nodes=出現局数、pv=各候補の後を最頻前例で辿った手順 (PvDepth手まで、
1手ごとに前例インデックスを引くだけなので高速)。score cp は常に0 —
勝率のcp換算は権威的に見えて意味を持たないため、情報は nodes列とpvに
限定する。`go infinite` (検討モード) では `stop` まで bestmove を保留する。
MultiPV / PvDepth / DbPath / SyncFile / OutputEncoding は USI option で変更可能。

前例がない場合は `info string 前例なし` + `bestmove resign`。resignを選んだ
理由: USIで合法な応答のうち「この局面に知識がない」ことを誤解なく伝えられる
唯一の値であり (適当な合法手を返すと前例と誤読される)、検討モードでは
bestmoveは表示に影響しない。対局モードで使えば前例が尽きた時点で投了する
「定跡専用エンジン」として自然に振る舞う。

**依存は標準ライブラリのみ (Python 3.10+)。** 当初は cshogi バックエンドを
予定していたが、純Python実装で実用充分な速度が出ており、ネイティブ拡張への
依存は配布性を損なうため採用しない。取り込み速度が問題になる規模では
マルチプロセス化 (ファイル単位で並列パース) で対応する。

## 棋譜の復元 (kifudb/export.py)

DBは自己完結なので、元ファイルを削除する運用でも前例ビューアのダブル
クリックからCSA棋譜を復元してローカルで開ける。復元は `games.moves` の
指し手列が源泉のため**手順として厳密**で、千日手の繰り返し手順もそのまま
再現される (局面索引から作るわけではないので手順が壊れることはない)。
復元→再取り込みで指し手・勝敗・終局理由・評価値・読み筋が完全一致する
ことをラウンドトリップテストで保証している (千日手・宣言勝ち・反則・
持将棋・駒落ち・KIF由来を含む522局で検証)。

復元できるもの: 全指し手、初期局面、対局者名、$EVENT、開始日時、
終局理由と勝敗 (%トークン+summary行)、評価値・読み筋。
復元できないもの (保存していない): 消費時間 (T行)、レーティング行等の
ヘッダーコメント、フリーテキストコメント、KIF固有ヘッダー (棋戦・場所
・持ち時間)、±32767を超える評価値の原値、255手超の読み筋の後半。

コメントのスロット規約は「p手目とp+1手目の間 = p手時点の局面」だが、
一部エンジン (WCSC35のkatsudon等) は自分の指し手の直後に指す前の局面の
解析を書く。この流儀はPVが直前の自手から始まる (=物理的に不可能な予測)
ことで確実に検出でき、取り込み時に1スロット前へ自動補正する。

## 今後の順序

1. KI2の取り込み対応 (from座標がないため擬似合法手生成が必要) と
   7z等アーカイブ取り込み
2. KiriCompass 本体の作り直し時に query.py を接続

済: DB作成・更新GUI (ビューアの「DB更新...」ウィンドウ)、
大規模データでの運用 (100万局超のDBで検索・floodgate逐次更新を実運用中)。
