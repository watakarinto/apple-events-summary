---
name: apple-event
description: Apple Event の字幕から製品ごとの日本語まとめ記事を生成してローカルコミットする。
---

# /apple-event

Apple Event の字幕から製品ごとの日本語まとめ記事を生成してローカルコミットする。

## 使い方

```
/apple-event <YouTube URL>   # URLから字幕・画像を自動取得
/apple-event                 # inbox/transcript.txt を使う
```

## 手順

### 1. 入力の判定

**URLが引数にある場合：**
```bash
yt-dlp --write-auto-sub --sub-lang en --skip-download \
  --write-info-json -o "inbox/%(title)s" <URL>
```
- `inbox/*.vtt` が字幕ファイル
- `inbox/*.info.json` の `chapters` フィールドをチャプター情報として使う（あれば）
- 日付は **JST（日本時間）** でタイムスタンプから取得する
  ```bash
  yt-dlp --skip-download --print "%(timestamp)s" <URL> \
    | xargs -I{} sh -c 'TZ=Asia/Tokyo date -r {} +%Y-%m-%d'
  ```

**URLがない場合：**
- `inbox/transcript.txt` を字幕として使う
- チャプター情報はなしとして扱う
- **日付の取得**：transcript.txt の1行目がYouTube URLの場合、yt-dlpでJSTで取得する
  ```bash
  URL=$(head -1 inbox/transcript.txt)
  yt-dlp --skip-download --print "%(timestamp)s" "$URL" \
    | xargs -I{} sh -c 'TZ=Asia/Tokyo date -r {} +%Y-%m-%d'
  ```
  URLがない、または取得できない場合は `1970-01-01` を仮置きしてユーザーに確認する
- transcript.txt の日本語タイムスタンプ形式（`0:2727 秒text`）はVTTではないので、行頭の `^\d+:\d+(?:\d+\s*分\s*\d+\s*秒|\d+\s*秒)` パターンを除去してテキストを抽出する

### 2. 字幕の読み込み

- VTT形式の場合はタイムスタンプ行を除去してテキストのみ抽出
- 重複行（VTTの仕様で同じ行が続く）を除去して読みやすくする

### 3. ストーリーボード取得とモザイクスライド特定（URLがある場合）

**URLがない場合はこのステップをスキップする。**

#### 3-1. スクリプトでストーリーボード取得・フレーム抽出・グリッド作成を一括実行

```bash
python3 .claude/skills/apple-event/extract_slides.py <URL>
```

このスクリプトが以下をすべて行う：
- ストーリーボード（sb0）を取得
- フレームを `inbox/storyboard_frames/` に抽出
- 各チャプターの末尾30%フレームをグリッド画像化 → `inbox/chapter_grids/{slug}.jpg`
- チャプター情報をJSON形式でstdoutに出力（タイムスタンプ・グリッドパス）

スクリプトがエラーを出した場合は内容を確認して対処し、このステップをスキップして画像なしで記事生成に進む。

#### 3-2. チャプターグリッドを確認 → タイムスタンプを特定しスカウトDL

3-1 で作成した `inbox/chapter_grids/{slug}.jpg` を Read ツールで表示し、
モザイク（機能一覧）スライドが映っているフレームのタイムスタンプ（グリッドに `t{秒}` 形式で表示）を特定する。

- 見つかった場合はそのタイムスタンプ（整数秒）を `slug:ts` として使う
- 見つからない場合は JSON 出力の `recommended_ts`（チャプター終端-90秒）を初期値として試す
- Introduction チャプターはモザイクスライドがないためスキップする

```bash
python3 .claude/skills/apple-event/extract_slides.py download <URL> \
  '<chapter_slug>:<ts>' \
  '<chapter_slug>:<ts>' \
  ...
```

スクリプトが以下を行う：
- 各チャプターの指定タイムスタンプ ±120秒を**360p以下で並列DL**（スカウト）
- 0.5fps でフレーム抽出（120フレーム）
- スカウトグリッド画像を `inbox/section_frames/{slug}_scout_grid.jpg` に保存
- タイムスタンプメタデータを `inbox/section_frames/{slug}_scout.json` に保存

**なぜ2段階か：** HLSストリームとストーリーボードのタイムスタンプは製品ごとに15〜80秒ずれることがある。±120sの広いウィンドウで確実にモザイクを捉え、確認後に1080pで精密DLする。

#### 3-3. スカウトグリッドを確認 → フレーム番号を特定して本番DL

`inbox/section_frames/{slug}_scout_grid.jpg` を確認し、モザイクスライドのフレーム番号（グリッドの連番）を特定する。

```bash
python3 .claude/skills/apple-event/extract_slides.py finalize <URL> \
  '<chapter_slug>:<frame_num>' \
  '<chapter_slug>:<frame_num>' \
  ...
```

スクリプトが以下を行う：
- スカウトメタデータからフレーム番号→実秒数を計算（`ts_start + frame_num - 1`）
- その秒数 ±10秒を**1080pで並列DL**（20フレームのみ）
- フレームグリッド画像を `inbox/section_frames/{slug}_grid.jpg` に保存

#### 3-4. 本番グリッドを確認 → 最終フレームを選択

`inbox/section_frames/{slug}_grid.jpg`（20フレーム）を確認し、アニメーション完成状態のフレームを選ぶ。

特定したフレーム（例: フレーム番号 `15` なら `inbox/section_frames/{slug}/frame_015.jpg`）を
`inbox/images/{product}-overview.jpg` にコピーする。このファイルは 1920x1080 の高解像度版。

「なし」の場合はそのチャプターの画像をスキップし、記事に画像挿入しない。

### 4. 製品ごとに日本語まとめを生成

以下のプロンプトでClaudeが処理する：

```
以下はApple Eventの英語字幕です。

[チャプター情報がある場合]
チャプター：
{chapters}

字幕：
{transcript}

[取得した画像がある場合]
各製品のまとめスライド画像：
{product}: {image_path}
...

指示：
1. 字幕を読み、製品・トピックごとにセクションを分割する
   - チャプターがあればそれを優先ヒントにする
   - なければ "Now, let's talk about..." などの切り替え発話を手がかりにする
2. 各セクションを英語で要点をまとめる（数値・チップ名・価格は字幕の該当箇所を根拠に正確に書く）
3. その要点をもとに、以下のテンプレートに沿って日本語記事を書く

**粒度の方針（最重要）：**
- 読者は **Apple信者・デベロッパ**。ハイレベルなサマリではなく、字幕で言及された機能・数値・デモ・API名を**すべて拾って詳報として書く**
- 「〇〇が改善された」で終わらせず、「何がどう変わったか・具体的な数値・仕組み」まで書く
  - 例：「アプリ起動が速くなった」→「iPhoneとiPadのアプリ起動が最大30%速くなった。使いそうなデータをあらかじめ読み込ませることで瞬時に使えるようにした」
- WWDCなど開発者向けイベントでは、API名・フレームワーク名・アーキテクチャの説明も省略しない
- プラットフォームごとに挙動が異なる場合は表にまとめて全プラットフォームを列挙する
- 一つのセクションに複数のサブ機能があれば、`###` 見出しで分けて各サブ機能を段落で説明する

**表現の方針：**
- 原文（字幕）の言い回しや主張を忠実に再現する。誇張・評価の上乗せはしない
- Appleが「最高」「革新的」と言っていれば書いてよいが、自分で「驚異的」「圧倒的」などと加えない
- 数値の比較（〇〇比△△%向上など）は字幕に根拠がある場合のみ書く
- 編集コメント（「〇〇が気になる」など）は最小限にとどめる

## 記事テンプレート

---
title: "{イベント名} まとめ"
date: {JST（日本時間）での開催日 YYYY-MM-DD}
draft: false
tags: [{製品カテゴリ}, ...]
# タグの方針：
# - 製品カテゴリのみ（iPhone / Mac / iPad / Apple Watch / AirPods / Vision Pro など）
# - 具体的なモデル名・イベント名・年はタグにしない
summary: "一言でイベントの概要（読者がクリックしたくなる文章）"
---

{イベント全体の冒頭まとめ。日付・場所・今年のキーワード・発表製品を2〜3文で。}

---

## {製品名1}

[まとめスライド画像がある場合]
![{製品名} 機能まとめ]({product}-overview.jpg)
*出典：Apple*

{この製品の「何がすごいか」を1〜2文で。読者が続きを読みたくなる導入。}

### {主要トピック見出し}

{短い段落で説明。専門用語は使ってよいがApple信者向けの温度感で書く。}
{スペックは文中に自然に織り込む。比較数値（〇〇比△△%向上など）は積極的に入れる。}

{複数の項目を並べる場合は箇条書き：}
- **{項目名}**：{説明}
- **{項目名}**：{説明}

{スペックや価格の比較は表で：}
| {列1} | {列2} |
|-------|-------|
| {値}  | {値}  |

### 価格・発売日

| モデル | 価格 |
|--------|------|
| {モデル名} | {価格} |

{予約開始日}予約開始、**{発売日}発売**。

---

{他の製品も同じ構成で繰り返す}

---

## まとめ

{全製品の価格・発売日を1つの表にまとめる}

| 製品 | 価格 | 発売日 |
|------|------|--------|
| {製品名} | {価格} | {発売日} |

{締めの一言。どの製品が特に注目か、どう選ぶかなどの軽い編集コメント。}
```

### 5. inbox/images/ の振り分け（手動画像がある場合）

ストーリーボード取得分以外に `inbox/images/` にファイルがあれば、各画像をVisionで確認して：
- どの製品セクションの画像かを判定
- 内容を反映した英語のケバブケース名にリネームして Page Bundle ディレクトリにコピー
  - ルール：`{カテゴリ}-{トピック}.{拡張子}`（小文字・ハイフン区切り・英語）
  - 例：`IMG_0012.png` → `iphone-variable-aperture.png`
  - 同じカテゴリ・トピックの画像が複数ある場合のみ末尾に `-02`、`-03` を付ける
  - カテゴリ例：`iphone` / `ipad` / `mac` / `apple-watch` / `airpods` / `vision-pro`
- 該当セクションの適切な位置に挿入（紹介文の直後など文脈に合う場所）

```markdown
![{日本語キャプション}]({renamed-filename})
*出典：Apple*
```

### 6. MDファイルの保存

Page Bundle 形式で保存する。**以下の順番で実行すること。**

```bash
# ① ディレクトリ作成
mkdir -p content/posts/{YYYY-MM-DD}-{slug}/

# ② inbox/images/ にある画像をすべてコピー（-overview.jpg 含む）
cp inbox/images/* content/posts/{YYYY-MM-DD}-{slug}/ 2>/dev/null || true

# ③ index.md を保存（Write ツールで作成）
```

最終的な構造：
```
content/posts/{YYYY-MM-DD}-{slug}/
├── index.md              ← 記事本文
├── {product}-overview.jpg  ← ストーリーボードから取得したまとめスライド
├── {other-image}.png     ← inbox/images/ から振り分けた画像
└── ...
```

- slug はイベント名から生成（例：`wwdc-2026`）

### 7. ローカルコミット

```bash
git add content/posts/{YYYY-MM-DD}-{slug}/
git commit -m "{イベント名} まとめ記事を追加"
```

**pushはしない。** コミット完了をユーザーに伝えて終了する。

## inbox/ の後片付け

コミット後に実行（中間ファイルをすべて削除）：

```bash
# 字幕・storyboard・中間ファイルを削除
rm -f inbox/*.vtt inbox/*.json inbox/*.mhtml
rm -rf inbox/storyboard_frames/ inbox/chapter_grids/ inbox/sections/ inbox/section_frames/

# transcript.txt の中身をクリア（ファイル自体は残す）
> inbox/transcript.txt

# images 内のファイルを削除（.gitkeep は残す）
find inbox/images -not -name '.gitkeep' -not -path 'inbox/images' -delete
```

字幕ファイルは著作権上、長期保存しない（PLANの法律面の整理より）。
