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

#### 3-1. モザイクスライドを自動抽出（推奨）

```bash
python3 .claude/skills/apple-event/extract_slides.py auto <URL>
```

以下をすべて自動実行する（ストーリーボードが未取得なら自動で取得してから進む）：
- ストーリーボードから各チャプターのモザイク候補タイムスタンプを自動検出（色多様性・エッジ密度スコアリング）
- スカウト DL（360p ±60s、0.5fps、チャプター境界内にクランプ）→ 自動フレーム選択
- 本番 DL（1080p ±10s）→ 自動フレーム選択
- `inbox/images/{slug}-overview.jpg` に自動コピー
- stdout に結果 JSON を出力（`image` パス・`final_frame` 番号・`grid_path`）

スクリプトがエラーを出した場合は内容を確認して対処し、このステップをスキップして画像なしで記事生成に進む。

完了後、stdout JSON の `grid_path` の画像を Read ツールで確認し、意図通りのモザイクスライドが取れているか検証する。問題があれば 3-2 で手動補正する。

特定チャプターのタイムスタンプを手動指定したい場合（スコアリングより精度が高い場合など）：
```bash
python3 .claude/skills/apple-event/extract_slides.py auto <URL> \
  '<chapter_slug>:<ts>' ...
```

#### 3-2. 手動補正（自動検出が失敗した製品のみ）

自動検出結果を確認後、問題のある製品だけ手動で再実行する。

```bash
# スカウトからやり直す場合
python3 .claude/skills/apple-event/extract_slides.py download <URL> '<slug>:<ts>'
# → inbox/section_frames/{slug}_scout_grid.jpg を確認し frame_num を特定
python3 .claude/skills/apple-event/extract_slides.py finalize <URL> '<slug>:<frame_num>'
# → inbox/section_frames/{slug}_grid.jpg を確認し最良フレームを特定
```

特定したフレーム（例: `frame_015.jpg`）を手動でコピーする：
```bash
cp inbox/section_frames/{slug}/frame_015.jpg inbox/images/{product}-overview.jpg
```

「なし」の場合はそのチャプターの画像をスキップし、記事に画像挿入しない。

### 4. 製品ごとに日本語まとめを生成（並列APIスクリプト）

Anthropic API を直接並列呼び出しするスクリプトで製品セクションを生成し、最後に組み立てる。

#### 4-1. スクリプトで並列生成

```bash
python3 .claude/skills/apple-event/generate_article.py
```

以下を自動実行する：
- `inbox/*.vtt` から各チャプターの字幕を時間範囲で抽出
- 全製品を `claude-sonnet-4-6` API に並列（最大7同時）でリクエスト
- 各製品セクションを `inbox/sections_md/{slug}.md` に保存
- stdout に `{"sections": [...]}` を出力（チャプター順）

スクリプト完了後、生成された各 `.md` ファイルを Read ツールで確認し、内容が正確かチェックする。問題があるセクションは修正してから次のステップへ進む。

#### 4-2. 記事を組み立てる

sections_md の内容が揃ったら、以下の順で1つのファイルに組み立てる：

1. **frontmatter + 冒頭まとめ**（自分で生成。全字幕を参照して日付・場所・発表製品を2〜3文）
2. **各製品セクション**（`inbox/sections_md/{slug}.md` をチャプター順に結合）
3. **`## まとめ`**（自分で生成。全製品の価格・発売日を1表にまとめ + 締めの一言）

frontmatter テンプレート：

```markdown
---
title: "{イベント名} まとめ"
date: {JST（日本時間）での開催日 YYYY-MM-DD}
draft: false
tags: [{製品カテゴリ}, ...]
# タグの方針：製品カテゴリのみ（iPhone / Mac / iPad / Apple Watch / AirPods / Vision Pro など）
summary: "一言でイベントの概要（読者がクリックしたくなる文章）"
---

{イベント全体の冒頭まとめ。日付・場所・今年のキーワード・発表製品を2〜3文で。}

---
```

まとめセクションテンプレート（自分で生成）：

```markdown
---

## まとめ

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
