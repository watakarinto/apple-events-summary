---
name: apple-event
description: Apple Event の字幕から製品ごとの日本語まとめ記事を生成してローカルコミットする。
---

# /apple-event

Apple Event の YouTube 動画から、製品ごとの日本語まとめ記事（カード状まとめスライドの画像付き）を生成し、ローカルコミットする。

## 使い方

```
/apple-event <YouTube URL>   # 通常（目安 3〜5分）
/apple-event                 # inbox/transcript.txt を使う
```

## 速度のための原則（厳守）

- 重い処理は `pipeline.py` に任せる。メインは**字幕を Read しない**。**記事本文を打ち直さない**（組み立ては `assemble`）
- ライターは **1つのメッセージで全員同時に起動**し、完了を待たずに `slides` へ進む
- 確認シートの Read は1回だけ。差し替えのコマンドは1回の Bash にまとめる

以下、`P <cmd>` は `uv run .claude/skills/apple-event/pipeline.py <cmd>` の略。プロジェクトルートで実行する。

## 手順（URL あり）

### 1. 字幕と info を取得（10秒前後）

```bash
P fetch <URL>
```

stdout の JSON に以下が入っている。
- `title`、`date`（JST の開催日）、`post_dir`（記事ディレクトリ名の候補）
- `chapters[]`：製品チャプターごとの `title` / `slug` / `transcript` / `part`
- `intro_transcript`

Introduction などの製品以外のチャプターは除外済み。手動字幕があればそれを使い、無ければ自動字幕を使う。

### 2. ライターを全員同時に起動（1メッセージ）

`chapters` の数だけ Agent を **1つのメッセージで** 起動する。`subagent_type: apple-event-writer` を指定し、prompt は次の3行だけにする（字幕の中身は貼らない。ライターが自分で読み、`part` に書き出す）。

```
チャプター: {title}
字幕: {transcript}
出力: {part}
```

起動したら待たずに 3 へ進む。

- ライターはチャプター内の**製品ごとに** `## {製品名}` セクションを書く（例：チャプター「AirPods」→ AirPods 4／AirPods Max／AirPods Pro 2）
- 執筆ルール（構成・セクションの分け方・書かないもの）は `.claude/skills/apple-event/writer_rules.md` にあり、ライターが毎回最初に読む。記事の書き方を変えるときはこのファイルを直す
- `apple-event-writer` が使えない場合（セッション途中でエージェント定義を作成・変更した場合など）は、`subagent_type: general-purpose`・`model: sonnet`・`effort: medium` で起動する。prompt の先頭に「最初に .claude/skills/apple-event/writer_rules.md を Read し、そのルールに厳密に従う」と書く

### 3. まとめスライドを抽出（20〜30秒）

```bash
P slides
```

- 240p SDR の映像全体を解析し、製品チャプターごとにカード状まとめスライドの候補を最大3つ（c1〜c3）出す
- c1 の 1080p SDR 静止画を `inbox/images/{image_slug}-overview.jpg` に保存する
- 確認シートを `inbox/review_sheet.jpg` に出力する

失敗した場合は画像なしで続行する（`assemble` は画像が無い製品には挿入しない）。

### 4. 確認シートを1回だけ Read する

`inbox/review_sheet.jpg` を Read し、製品ごとに左の大きい画像（c1、緑枠）が**その製品のカード状まとめスライド**かを判定する。

- ○：角丸のカードが格子状に並び、全カードが揃っていて四辺に余白がある
- ×：製品単体の写真、合成カット、UI 画面、写真ギャラリー、タイトルスライド、組み上がり途中やズーム途中のもの

直す必要がある製品だけ、1回の Bash でまとめて実行する。

```bash
P pick <slug> c2            # c2 に差し替え
P pick <slug> none          # 画像なし
P pick <slug> c2 --extra    # 追加画像として {image_slug}-overview-2.jpg に保存（c1 はそのまま）
```

- `--extra` は、1チャプターに複数製品があり（例：AirPods 4 と AirPods Pro、Series と Ultra）、c2 以降が別製品のまとめスライドである場合に使う。どの画像がどの製品のものかを覚えておき、手順6で `place` する
- c1〜c3 のどれも違う場合は `P sheet <slug> [開始秒 終了秒]` で一覧シートを作り、Read して秒数を決め、`P pick <slug> <秒>` を実行する
- `[LOW CONFIDENCE]` が付いた製品は特に注意して見る
- `pick` の出力の `match` が 0.9 未満なら、その画像を Read して確認する

### 5. ライターの完了を待つ

各ライターの完了通知（製品ごとに `- {製品名}｜価格: …｜発売日: …` の1行）が全員分そろうまで待つ。ポーリングは不要。失敗したライターは同じ prompt で起動し直す。

### 6. 画像の配置、ヘッダと価格表（メインが書く本文はここだけ）

まず `grep "^## " inbox/article_parts/*.md` で製品セクションを確認する。

**画像の配置：** 1チャプターに1製品なら何もしなくてよい（`assemble` が c1 の画像をセクションの先頭に自動で入れる）。1チャプターに複数製品がある場合は、そのチャプターの画像（c1 と `--extra` で追加したもの）を、すべて対応する製品に明示的に置く。まとめスライドが無い製品には置かない。

```bash
P place airpods-overview.jpg "AirPods 4"
P place airpods-overview-2.jpg "AirPods Pro 2"   # 見出しは前方一致。置き直すと移動する
```

**ヘッダと価格表：**
- `inbox/article_parts/00_header.md`：frontmatter と導入1〜2文だけを書く（製品の一覧はページの目次が兼ねるので書かない）。必要なら `intro_transcript` を Read してよい（短い）
- `inbox/article_parts/99_summary.md`：`## 価格・発売日` の表だけを書く（ライターの返答から作る）。締めのコメントは書かない。価格はドルのまま書く。「日本円（参考）」の列と注記は、`assemble` が発表日の為替レートで自動的に足す（レートは `slides` が取得済み。自分で計算しない）
- `---` 区切りは書かない（`assemble` が入れる）
- 登壇者の名前や誰が話したか、宣伝文句は書かない。事実だけを書く
- ライターの返答で価格・発売日が「言及なし」の製品は、発売情報が別チャプター（最後の製品など）でまとめて告知されていることが多い。`grep -n -i "pre-order\|available" inbox/chapter_transcripts/*.txt` で確認し、見つかれば価格表に反映する

**手動画像（後述）** は、該当パートの文脈に合う位置に Edit で挿入する。

```markdown
![{日本語キャプション}]({ファイル名})
*出典：Apple*
```

`00_header.md` のテンプレート：

```markdown
---
title: "{イベント名} まとめ"
date: {date}
draft: false
tags: [{製品カテゴリ}, ...]
# タグは製品カテゴリのみ（iPhone / Mac / iPad / Apple Watch / AirPods / Vision Pro など）
summary: "イベントの概要を一言で（読者がクリックしたくなる文章）"
---

{日付・場所・今年のテーマを1〜2文で。}
```

`99_summary.md` のテンプレート（製品は発表順＝記事のセクションと同じ順に並べる。モデルごとに価格が違えば行を分ける）：

```markdown
## 価格・発売日

| 製品 | 価格 | 発売日 |
|------|------|--------|
| {製品名} | {価格} | {発売日} |
```

### 7. 組み立て・コミット・後片付け（1回の Bash で）

```bash
P assemble {post_dir} \
  && git add content/posts/{post_dir}/ \
  && git commit -m "{イベント名} まとめ記事を追加" \
  && P clean
```

- `assemble` は、ヘッダ・全製品セクション・価格表を `---` でつないで `content/posts/{post_dir}/index.md` を作り、`inbox/images/` の画像をコピーする。`place` していないチャプターの c1 画像は、そのパートの最初の `##` の直後に自動で入れる
- パートが足りないとエラーになる
- `post_dir` は `fetch` が出した候補を使う（例：`2024-09-10-apple-event-september-2024`）。おかしい場合だけ直す
- **push はしない。** コミットが完了したことをユーザーに伝えて終了する

## inbox/images/ の手動画像

`inbox/images/` に `*-overview*.jpg` 以外の画像があれば、各画像を Vision で確認する。

- どの製品セクションの画像かを判定する
- 内容を表す英語のケバブケース名 `{カテゴリ}-{トピック}.{拡張子}` にリネームする（例：`IMG_0012.png` → `iphone-variable-aperture.png`）
  - カテゴリの例：`iphone` / `ipad` / `mac` / `apple-watch` / `airpods` / `vision-pro`
  - 同じカテゴリ・トピックの画像が複数ある場合だけ、末尾に `-02`、`-03` を付ける
- 手順6で該当パートの文脈に合う位置に挿入する

## URL なし（inbox/transcript.txt）の場合

- 1行目が YouTube URL なら、その URL で通常の手順を実行する
- URL が無い場合は、メインが `inbox/transcript.txt` を読んで記事を直接書く（画像なし）
  - 行頭のタイムスタンプ（`^\d+:\d+(?:\d+\s*分\s*\d+\s*秒|\d+\s*秒)`）は除去する
  - 日付は `1970-01-01` を仮置きし、ユーザーに確認する
- 保存先は `content/posts/{YYYY-MM-DD}-{slug}/index.md`。その後、手順7の git コマンドと `P clean` を実行する

## 補足

- 映像は yt-dlp の既定（HDR 優先）を使わず、必ず SDR を取得する。HDR をそのまま JPEG にすると白っぽく暗い画像になるため
- 動画や字幕は著作権上、長期保存しない。`P clean` で inbox の中間ファイルを削除する
